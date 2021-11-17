import torch
import pandas as pd
import numpy as np
import pickle
from tqdm import tqdm
import random

import matplotlib.pyplot as plt

from sklearn.metrics import roc_curve
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split

import xgboost

print(torch.cuda.is_available())
if torch.cuda.is_available():  
  dev = "cuda:0" 
else:  
  dev = "cpu" 

def setSeed(seed):
  torch.manual_seed(seed)
  random.seed(seed)
  np.random.seed(seed)

class Model:
  def __init__(self, train_features, train_masses):
    self.train_features = train_features
    self.train_masses = train_masses
    self.initModel()

  def equaliseWeights(self, X, w, y, norm=None):
    #equalise weights amongst different masses
    avg_sig_sumw = sum(w[y==1]) / len(self.train_masses)
    avg_bkg_sumw = sum(w[y==0]) / len(self.train_masses)
    for m in self.train_masses:
      w[(y==1)&(X[:,-1]==m)] = w[(y==1)&(X[:,-1]==m)] * (avg_sig_sumw / sum(w[(y==1)&(X[:,-1]==m)]))
      w[(y==0)&(X[:,-1]==m)] = w[(y==0)&(X[:,-1]==m)] * (avg_sig_sumw / sum(w[(y==0)&(X[:,-1]==m)]))

    #now equalise sig vs bkg
    if norm==None:
      norm = sum(w[y==1])
    
    w[y==1] = w[y==1] * (norm / sum(w[y==1]))
    w[y==0] = w[y==0] * (norm / sum(w[y==0]))
    return w

  def reweightMass(self, X, y, w, m, sf):
    w[(y==1)&(X[:,-1]==m)] = w[(y==1)&(X[:,-1]==m)] * sf
    w[(y==0)&(X[:,-1]==m)] = w[(y==0)&(X[:,-1]==m)] * sf
    return w

  def cullSignal(self, X, y, w):
    #keep as much signal as there is background
    nsig = sum(y==1)
    nbkg = sum(y==0)
    signal_indices = np.arange(nsig+nbkg)[y==1]
    bkg_indices = np.arange(nsig+nbkg)[y==0]
    keep_signal_indices = np.random.choice(signal_indices, nbkg, replace=False)    
    selection = np.concatenate([bkg_indices, keep_signal_indices])
    return X[selection], y[selection], w[selection]

  def printNumAndWeight(self, y, w):
    print(" nsig = %d"%sum(y==1))
    print(" nbkg = %d"%sum(y==0))
    print(" sum wsig = %f"%sum(w[y==1]))
    print(" sum wbkg = %f"%sum(w[y==0]))

  def printSampleSummary(self, X_train, y_train, X_test, y_test, w_train, w_test):
    print("Training set:")
    self.printNumAndWeight(y_train, w_train)
    for m in self.train_masses:
      s = X_train[:,-1] == m
      print(" m=%d"%int(m*1000))
      self.printNumAndWeight(y_train[s], w_train[s])

    print("Test set:")
    self.printNumAndWeight(y_test, w_test)
    for m in self.train_masses:
      s = X_test[:,-1] == m
      print(" m=%d"%int(m*1000))
      self.printNumAndWeight(y_test[s], w_test[s])

  def shuffleBkg(self, X_train, y_train, X_test, y_test):
    X_train[y_train==0,-1] = np.random.choice(self.train_masses, len(X_train[y_train==0][:,-1]))
    X_test[y_test==0,-1] = np.random.choice(self.train_masses, len(X_test[y_test==0][:,-1]))
    return X_train, X_test

  def inflateBkgWithMasses(self, X, y, w):
    X_sig, y_sig, w_sig = X[y==1], y[y==1], w[y==1]
    X_bkg, y_bkg, w_bkg = X[y==0], y[y==0], w[y==0]
    
    #lists to hold signal samples and copies of bkg before concatenating
    Xs, ys, ws = [X_sig], [y_sig], [w_sig]    
    
    for m in self.train_masses:
      X_bkg_c, y_bkg_c, w_bkg_c = X_bkg.copy(), y_bkg.copy(), w_bkg.copy()
      X_bkg_c.loc[:, "mass"] = m
      
      Xs.append(X_bkg_c)
      ys.append(y_bkg_c)
      ws.append(w_bkg_c)

    X, y, w = pd.concat(Xs, ignore_index=True), pd.concat(ys, ignore_index=True), pd.concat(ws, ignore_index=True)
    return X, y, w

  def getROC(self, X, y, weight=None):
    predictions = self.predict(X)
    fpr, tpr, t = roc_curve(y, predictions, sample_weight=weight)
    try:
      auc = roc_auc_score(y, predictions, sample_weight=weight)
    except:
      auc = np.trapz(tpr, fpr)
    return fpr, tpr, auc

class ParamNN(Model):
  def __init__(self, train_features, train_masses, loss="BCE"):
    if loss == "BCE":
      self.loss_function = self.BCELoss
    elif loss == "MSE":
      self.loss_function = self.MSELoss
    else:
      raise Exception("The %s loss function does not exist."%loss)

    Model.__init__(self, train_features, train_masses)

  def initModel(self):
    # self.model = torch.nn.Sequential(
    #               torch.nn.Linear(len(self.train_features),1),
    #               torch.nn.ELU(),
    #               torch.nn.Flatten(0,1),
    #               torch.nn.Sigmoid()
    #               )

    # self.model = torch.nn.Sequential(
    #               torch.nn.Linear(len(self.train_features),10),
    #               torch.nn.ELU(),
    #               torch.nn.Linear(10,10),
    #               torch.nn.ELU(),
    #               torch.nn.Linear(10,1),
    #               torch.nn.Flatten(0,1),
    #               torch.nn.Sigmoid()
    #             )

    nfeatures = len(self.train_features)
    self.model = torch.nn.Sequential(
                  torch.nn.Linear(nfeatures,int(nfeatures/2)),
                  torch.nn.Dropout(0.1),
                  torch.nn.ELU(),
                  torch.nn.Linear(int(nfeatures/2),int(nfeatures/2)),
                  torch.nn.Dropout(0.1),
                  torch.nn.ELU(),
                  torch.nn.Linear(int(nfeatures/2),1),
                  torch.nn.Flatten(0,1),
                  torch.nn.Sigmoid()
                )

  def BCELoss(self, input, target, weight):
    x, y, w = input, target, weight
    #log = lambda x: torch.clamp(torch.log(x), min=-100, max=100)
    log = lambda x: torch.log(x+1e-16)
    return torch.mean(-w * (y*log(x) + (1-y)*log(1-x)))

  def MSELoss(self, input, target, weight):
    return torch.mean(weight * (input - target) ** 2)

  def getTotLoss(self, X, y, w, batch_size):
    losses = []
    for batch_X, batch_y, batch_w in self.getBatches(X, y, w, batch_size):
      loss = self.loss_function(self.model(batch_X), batch_y, batch_w)
      losses.append(loss.item())
    return sum(losses)

  def getBatches(self, X, y, w, batch_size, shuffle=False):
    if shuffle:
      shuffle_ids = np.random.permutation(len(X))
      X_sh = X[shuffle_ids].copy()
      y_sh = y[shuffle_ids].copy()
      w_sh = w[shuffle_ids].copy()
    else:
      X_sh = X.copy()
      y_sh = y.copy()
      w_sh = w.copy()
    for i_picture in range(0, len(X), batch_size):
      batch_X = X_sh[i_picture:i_picture + batch_size]
      batch_y = y_sh[i_picture:i_picture + batch_size]
      batch_w = w_sh[i_picture:i_picture + batch_size]
    
      X_torch = torch.tensor(batch_X, dtype=torch.float).reshape(-1, X.shape[1]).to(dev)
      y_torch = torch.tensor(batch_y, dtype=torch.float).to(dev)
      w_torch = torch.tensor(batch_w, dtype=torch.float).to(dev)

      yield X_torch, y_torch, w_torch

  def shouldEarlyStop(self, losses, min_epoch=10, grace_epochs=5, tol=0.01):
    """
    Want to stop if seeing no appreciable improvment.
    Check 1. Was the best score more than grace_epochs epochs ago?
          2. If score is improving, has it improved by more than
             tol percent over grace_epochs?
    """

    n_epochs = len(losses)

    if n_epochs < min_epoch:
      return False

    losses = np.array(losses)
    slosses = losses.sum(axis=1)

    #check to see if loss unstable
    if n_epochs > grace_epochs:
      any_unstable = False
      for i in range(len(self.train_masses)):
        best_loss = losses[:,i].min()        
        variation = losses[:,i][-grace_epochs:].max() - losses[:,i][-grace_epochs:].min()
        #print(variation, variation/best_loss)
        if variation/best_loss > tol:
          any_unstable = True
          break
      if any_unstable:
        return False

    #check if best loss happened a while ago
    best_loss = slosses.min()
    best_loss_epoch = np.where(slosses==best_loss)[0][0] + 1

    if n_epochs - best_loss_epoch > grace_epochs:
      print("Best loss happened a while ago")
      return True

    #check to see if any mass points making good progress
    if n_epochs > grace_epochs:
      any_make_good_improvement = False
      for i in range(len(self.train_masses)):
        best_loss = losses[:,i].min()        
        best_loss_before = losses[:,i][:-grace_epochs].min()
        if (best_loss_before-best_loss)/best_loss > tol:
          any_make_good_improvement = True
          break
      if not any_make_good_improvement:
        print("Not enough improvement")
        return True

    return False

  def shouldSchedulerStep(self, losses):
    n_epochs = len(losses)
    
    losses = np.array(losses)
    slosses = losses.sum(axis=1)
    best_loss = slosses.min()
    best_loss_epoch = np.where(slosses==best_loss)[0][0] + 1

    if ((n_epochs - best_loss_epoch) > 5) and ((n_epochs - self.last_step_epoch) > 5):
      self.last_step_epoch = n_epochs
      return True
    else:
      return False

  def updateLossPlot(self, train_loss, test_loss, lr):
    if not self.alreadyPlotting:
      plt.ion()
      self.figure, self.ax = plt.subplots()
      x = [i for i in range(1, len(train_loss)+1)]
      self.train_line, = self.ax.plot(x, train_loss, label="train")
      self.test_line, = self.ax.plot(x, test_loss, label="test")
      self.lr_text = self.ax.text(0.1, 1.05, "lr = %f"%lr, transform=self.ax.transAxes)
      self.step_text = self.ax.text(0.5, 1.05, "last scheduler step at epoch %d"%self.last_step_epoch, transform=self.ax.transAxes)
      plt.xlabel("epoch")
      plt.ylabel("loss")
      plt.legend()
      self.alreadyPlotting = True
    else:
      x = [i for i in range(1, len(train_loss)+1)]
      self.train_line.set_data(x, train_loss)
      self.test_line.set_data(x, test_loss)
      self.lr_text.set_text("lr = %f"%lr)
      self.step_text.set_text("last scheduler step at epoch %d"%self.last_step_epoch)
      self.ax.relim()
      self.ax.autoscale_view()
      self.figure.canvas.draw()    
      self.figure.canvas.flush_events()

  def train(self, X_train, y_train, X_test, y_test, w_train, w_test, max_epochs=100, batch_size=32, lr=0.1, min_epoch=10, grace_epochs=5, tol=0.01, gamma=1.0):
    self.alreadyPlotting = False
    self.last_step_epoch = 0
    
    X_train, y_train, w_train = self.inflateBkgWithMasses(X_train, y_train, w_train)
    X_test, y_test, w_test = self.inflateBkgWithMasses(X_test, y_test, w_test)
    
    X_train = X_train.to_numpy()
    X_test = X_test.to_numpy()
    y_train = y_train.to_numpy()
    y_test = y_test.to_numpy()
    w_train = w_train.to_numpy()
    w_test = w_test.to_numpy()

    #X_train, y_train, w_train = self.cullSignal(X_train, y_train, w_train)
    #X_test, y_test, w_test = self.cullSignal(X_test, y_test, w_test)

    w_train = self.equaliseWeights(X_train, w_train, y_train, norm=1000)
    w_test = self.equaliseWeights(X_test, w_test, y_test, norm=1000)
    
    #w_train = self.reweightMass(X_train, y_train, w_train, 0.3, 100)
    #w_test = self.reweightMass(X_test, y_test, w_test, 0.3, 100)

    self.printSampleSummary(X_train, y_train, X_test, y_test, w_train, w_test)

    optimizer = torch.optim.Adam(self.model.parameters(), lr=lr)
    #optimizer = torch.optim.AdamW(self.model.parameters(), lr=lr)
    scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, gamma=gamma)

    train_loss = []
    test_loss = []

    for i_epoch in tqdm(range(max_epochs)):
      try:
        #X_train, X_test = self.shuffleBkg(X_train, y_train, X_test, y_test)

        for batch_X, batch_y, batch_w in tqdm(self.getBatches(X_train, y_train, w_train, batch_size, shuffle=True), leave=False):
          loss = self.loss_function(self.model(batch_X), batch_y, batch_w)
          self.model.zero_grad()
          loss.backward()
          optimizer.step()
          
        trl = []
        tel = []
        for mass in self.train_masses:
          trl.append(self.getTotLoss(X_train[X_train[:,-1]==mass], y_train[X_train[:,-1]==mass], w_train[X_train[:,-1]==mass], 1024))
          tel.append(self.getTotLoss(X_test[X_test[:,-1]==mass], y_test[X_test[:,-1]==mass], w_test[X_test[:,-1]==mass], 1024))
        train_loss.append(trl)
        test_loss.append(tel)
        
        if self.shouldSchedulerStep(train_loss):
          scheduler.step()

        self.updateLossPlot(train_loss, test_loss, scheduler.get_last_lr()[0])

      except KeyboardInterrupt:
        break


      if self.shouldEarlyStop(test_loss, min_epoch=min_epoch, grace_epochs=grace_epochs, tol=tol):
        break

    print("Finished training")
      
    return train_loss, test_loss

  def predict(self, X, batch_size=32):
    X = X.to_numpy()
    predictions = []
    for i_picture in range(0, len(X), batch_size):
      batch_X = X[i_picture:i_picture + batch_size]
      X_torch = torch.tensor(batch_X, dtype=torch.float).reshape(-1, X.shape[1]).to(dev)
      predictions.append(self.model(X_torch).to('cpu').detach().numpy())
    return np.concatenate(predictions)

class BDT(Model):
  def initModel(self):
    self.model = xgboost.XGBClassifier()
    #self.model = xgboost.XGBClassifier(min_child_weight=0.5, subsample=0.5, gamma=0.5)

  def predict(self, X):
    X = X.to_numpy()
    y_pred = self.model.predict_proba(X)[:,1]
    return y_pred
  
  def train(self, X_train, y_train, X_test, y_test, w_train, w_test):
    X_train, y_train, w_train = self.inflateBkgWithMasses(X_train, y_train, w_train)
    X_test, y_test, w_test = self.inflateBkgWithMasses(X_test, y_test, w_test)
    
    X_train = X_train.to_numpy()
    X_test = X_test.to_numpy()
    y_train = y_train.to_numpy()
    y_test = y_test.to_numpy()
    w_train = w_train.to_numpy()
    w_test = w_test.to_numpy()

    #X_train, y_train, w_train = self.cullSignal(X_train, y_train, w_train)
    #X_test, y_test, w_test = self.cullSignal(X_test, y_test, w_test)

    w_train = self.equaliseWeights(X_train, w_train, y_train, norm=10000)
    w_test = self.equaliseWeights(X_test, w_test, y_test, norm=10000)
    
    #w_train = self.reweightMass(X_train, y_train, w_train, 0.3, 100)
    #w_test = self.reweightMass(X_test, y_test, w_test, 0.3, 100)

    self.printSampleSummary(X_train, y_train, X_test, y_test, w_train, w_test)

    self.model.fit(X_train, y_train, sample_weight=w_train)
