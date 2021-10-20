from _operator import itemgetter
from math import sqrt
import random
import time

from pympler import asizeof
import numpy as np
import pandas as pd
from math import log10
import scipy.sparse
from scipy.sparse.csc import csc_matrix
import theano
import theano.tensor as T
from datetime import datetime as dt
from datetime import timedelta as td

class SessionMF:
    '''
    RecurrentNeigborhoodModel( learning_rate=0.01, regularization=0.001, session_key = 'SessionId', item_key= 'ItemId', time_key= 'ItemId')

    Parameters
    -----------
    k : int
        Number of neighboring session to calculate the item scores from. (Default value: 100)
    sample_size : int
        Defines the length of a subset of all training sessions to calculate the nearest neighbors from. (Default value: 500)
    sampling : string
        String to define the sampling method for sessions (recent, random). (default: recent)
    similarity : string
        String to define the method for the similarity calculation (jaccard, cosine, binary, tanimoto). (default: jaccard)
    remind : bool
        Should the last items of the current session be boosted to the top as reminders
    pop_boost : int
        Push popular items in the neighbor sessions by this factor. (default: 0 to leave out)
    extend : bool
        Add evaluated sessions to the maps
    normalize : bool
        Normalize the scores in the end
    session_key : string
        Header of the session ID column in the input file. (default: 'SessionId')
    item_key : string
        Header of the item ID column in the input file. (default: 'ItemId')
    time_key : string
        Header of the timestamp column in the input file. (default: 'Time')
    '''
    
    def __init__( self, factors=100, batch=50, learn='adagrad', learning_rate=0.001, regularization=0.0001, dropout=0, skip=0, samples=0, activation='relu', objective='bpr', epochs=5, last_n_days=None, session_key = 'SessionId', item_key= 'ItemId', time_key= 'Time' ):
       
        self.factors = factors
        self.batch = batch
        self.learning_rate = learning_rate
        self.learn = learn
        self.regularization = regularization
        self.samples = samples
        self.dropout = dropout
        self.skip = skip
        self.epochs = epochs
        self.activation = activation
        self.objective = objective
        self.last_n_days = last_n_days
        self.session_key = session_key
        self.item_key = item_key
        self.time_key = time_key
        
        #updated while recommending
        self.session = -1
        self.session_items = []
        self.relevant_sessions = set()

        # cache relations once at startup
        self.session_item_map = dict() 
        self.item_session_map = dict()
        self.session_time = dict()
        
        self.item_map = dict()
        self.item_count = 0
        self.session_map = dict()
        self.session_count = 0
        
        self.floatX = theano.config.floatX
        self.intX = 'int32'
    
    def fit(self, data, items=None):
        '''
        Trains the predictor.
        
        Parameters
        --------
        data: pandas.DataFrame
            Training data. It contains the transactions of the sessions. It has one column for session IDs, one for item IDs and one for the timestamp of the events (unix timestamps).
            It must have a header. Column names are arbitrary, but must correspond to the ones you set during the initialization of the network (session_key, item_key, time_key properties).
            
        '''
        
        self.unique_items = data[self.item_key].unique().astype( self.intX )
        self.num_items = data[self.item_key].nunique()
        self.item_list = np.zeros( self.num_items )
        
        start = time.time()
        self.init_items(data)
        print( 'finished init item map in {}'.format(  ( time.time() - start ) ) )
        
        if self.last_n_days != None:
            
            max_time = dt.fromtimestamp( data[self.time_key].max() )
            date_threshold = max_time.date() - td( self.last_n_days )
            stamp = dt.combine(date_threshold, dt.min.time()).timestamp()
            train = data[ data[self.time_key] >= stamp ]
        
        else: 
            train = data
        
        self.init_sessions( train )
            
        start = time.time()
        self.init_model( train )
        print( 'finished init model in {}'.format(  ( time.time() - start ) ) )
        
        start = time.time()
        
        avg_time = 0
        avg_count = 0
                   
        for j in range( self.epochs ):
            
            loss = 0
            count = 0
            hit = 0
            
            batch_size = set(range(self.batch))
                    
            ipos = np.zeros( self.batch ).astype( self.intX )
            #ineg = np.zeros( self.batch ).astype( self.intX )
            
            finished = False
            next_sidx = len(batch_size)
            sidx = np.arange(self.batch)
            spos = np.ones( self.batch ).astype( self.intX )
            svec = np.zeros( (self.batch, self.num_items) ).astype( self.floatX )
            smat = np.zeros( (self.batch, self.num_items) ).astype( self.floatX )
            sci = np.zeros( self.batch ).astype( self.intX )
            scp = np.zeros( self.batch ).astype( self.intX )
                        
            while not finished:
                
                #rand = []
                #ran = np.random.random_sample()
                #ran2 =  np.random.choice(2,size=self.num_items,p=[self.dropout,1-self.dropout])
                items = []
                for i in range(self.batch):
                    
                    item_pos = self.session_map[ self.sessions[ sidx[i] ] ][ spos[i] ]
                    #if ran < self.skip and len(self.session_map[ self.sessions[ sidx[i] ] ]) > spos[i] + 1:
                    #    item_pos = self.session_map[ self.sessions[ sidx[i] ] ][ spos[i] + 1 ]
                        
                    item_current = self.session_map[ self.sessions[ sidx[i] ] ][ spos[i] - 1 ]
                    #prev = spos[i] - 1 if spos[i] - 1 > 0 else spos[i]
                    #item_prev = self.session_map[ self.sessions[ sidx[i] ] ][ prev ]
                    items.extend(self.session_map[ self.sessions[ sidx[i] ] ][ :spos[i] - 1 ])
                    
                    ipos[i] = item_pos
                    sci[i] = item_current
                    #scp[i] = item_current
                    svec[i][ sci[i] ] = spos[i]
                    #smat[i] = svec[i] / spos[i]
                    #smat[i] = smat[i] * ran2
                    
                    spos[i] += 1
                
                
                if self.samples > 0:
                    #additional = samples
                    additional = np.random.randint(self.num_items, size=self.samples).astype( self.intX )
                    stmp = time.time()
                    loss += self.train_model_batch( svec, sci, np.hstack( [ipos, additional] ), items )
                    avg_time += (time.time() - stmp)
                    avg_count += 1
                else:
                    loss +=  self.train_model_batch( svec, sci, ipos, scp )
                

                if np.isnan(loss):
                    print(str(j) + ': NaN error!')
                    self.error_during_train = True
                    return
                
                count += self.batch
                
                #HITRATE
#                 preds = self.predict_batch( smat, sci )
#                 preds += np.random.rand(*preds.shape) * 1e-8
#                 val_pos = preds.T[ipos].T.diagonal()
#                 hit += ( (preds.T > val_pos).sum(axis=0) < 20 ).sum()
                #HITRATE
                          
                for i in range(self.batch):
                    if len( self.session_map[ self.sessions[ sidx[i] ] ] ) == spos[i]: #session end
                        if next_sidx < len( self.sessions ):
                            spos[i] = 1
                            sidx[i] = next_sidx
                            svec[i] = np.zeros( self.num_items ).astype( self.floatX )
                            #smat[i] = np.zeros( self.num_items ).astype( self.floatX )
                            #sneg[i] = []
                            next_sidx += 1
                        else:
                            spos[i] -= 1
                            batch_size -= set([i])
                    
                    if len(batch_size) == 0:
                        finished = True
                            
                
                #if count % 10000 == 0 :
                #    print( 'finished {} of {} in epoch {} with loss {} / hr {} in {}s'.format( count, len(train), j, ( loss / count ), ( hit / count ), ( time.time() - start ) ) )
                
            print( 'finished epoch {} with loss {} / hr {} in {}s'.format( j, ( loss / count ), ( hit / count ), ( time.time() - start ) ) )
            
        print( 'avg_time_fact: ',( avg_time / avg_count ) )
        #self.train_model_batch.profile.summary()
    
    def fit2(self, data, items=None):
        '''
        Trains the predictor.
        
        Parameters
        --------
        data: pandas.DataFrame
            Training data. It contains the transactions of the sessions. It has one column for session IDs, one for item IDs and one for the timestamp of the events (unix timestamps).
            It must have a header. Column names are arbitrary, but must correspond to the ones you set during the initialization of the network (session_key, item_key, time_key properties).
            
        '''
        
        start = time.time()
        
        self.unique_items = data[self.item_key].unique().astype( self.intX )
        self.num_items = data[self.item_key].nunique()
        
        self.item_map = pd.Series(data=np.arange(self.num_items), index=self.unique_items )
        self.data = pd.merge(data, pd.DataFrame({self.item_key:self.unique_items, 'ItemIdx':self.item_map[self.unique_items].values}), on=self.item_key, how='inner')
        
        
        #sessionitemsize = self.data.groupby( ['SessionId','ItemId'] ).size()
        
        print( 'finished init item map in {}'.format(  ( time.time() - start ) ) )
        
        if self.last_n_days != None:
            
            max_time = dt.fromtimestamp( self.data[self.time_key].max() )
            date_threshold = max_time.date() - td( self.last_n_days )
            stamp = dt.combine(date_threshold, dt.min.time()).timestamp()
            train = self.data[ self.data[self.time_key] >= stamp ]
        
        else: 
            train = self.data

        train.sort_values( [self.session_key,self.time_key], inplace=True )
        train.reset_index(drop=True, inplace=True)
                
        offset_sessions = np.zeros(train[self.session_key].nunique()+1, dtype=np.int32)
        offset_sessions[1:] = train.groupby(self.session_key).size().cumsum()
        
        max_sidx = len(offset_sessions)-2
        
        itemsList = np.array( train.ItemIdx.astype( self.intX ) )
                    
        start = time.time()
        self.init_model( train )
        print( 'finished init model in {}'.format(  ( time.time() - start ) ) )
        
        avg_time = 0
        avg_count = 0
        
        tstart = time.time()
                
        for j in range( self.epochs ):
            
            loss = 0
            count = 0
            
            batch_size = set(range(self.batch))
            
            next_sidx = len(batch_size)
            
            finished = False
            
            fk = np.arange(self.batch)
            
            sidx = np.arange(self.batch)
            spos = np.ones( self.batch ).astype( self.intX )
            svec = np.zeros( (self.batch, self.num_items) ).astype( self.floatX )
            
            item_lists = {}
            for i in range(self.batch):
                item_lists[i] = []
            
            start = offset_sessions[sidx]
            end = offset_sessions[sidx+1]
            
            while not finished:
                
                item_pos = itemsList[ start + spos ] 
                item_current = itemsList[ start + spos - 1 ] 
                
                for i in range(self.batch):
                    item_lists[i].append( item_current[i] )
                
                items = [item for key in item_lists for item in item_lists[key]]
                                
                svec[fk, item_current] = spos
                smat = ( svec / spos[:,None] ).astype( self.floatX )
                    
                spos += 1
                
                stmp = time.time()
                if self.samples > 0:
                    additional = np.random.randint(self.num_items, size=self.samples).astype( self.intX )
                    loss += self.train_model_batch( smat, item_current, np.hstack( [item_pos, additional] ), items )
                else:
                    loss += self.train_model_batch( smat, item_current, item_pos, items )
                avg_time += (time.time() - stmp)
                avg_count += 1
                
                count += self.batch
                
                update = fk[end - (start + spos) <= 0]
                
                if len(update) > 0:
                    
                    for i in update:
                        item_lists[i] = []
                    
                    nexts = np.arange( next_sidx, next_sidx + len( update ) )
                    toohigh = np.arange( len(update) )[ nexts > max_sidx ]
                    next_sidx = nexts.max() + 1
                    
                    sidx[update] = nexts
                    over = fk[ sidx > max_sidx ]
                    batch_size -= set(over)
                    
                    nexts[toohigh] = max_sidx
                    
                    sidx[update] = nexts
                    start = offset_sessions[sidx]
                    end = offset_sessions[sidx+1]
                    svec[update] = np.zeros( self.num_items ).astype( self.floatX )
                    spos[update] = 1
                    
                    if len(batch_size) == 0:
                        finished = True

#                 if count % 10000 == 0 :
#                     print( 'finished {} of {} in epoch {} with loss {} in {}s'.format( count, len(train), j, ( loss / count ), ( time.time() - tstart ) ) )
                
            print( 'finished epoch {} with loss {} in {}s'.format( j, ( loss / count ), ( time.time() - tstart ) ) )
            
        print( 'avg_time_fact: ',( avg_time / avg_count ) )
        #self.train_model_batch.profile.summary()
        
    def init_model(self, train, std=0.01):
        
        self.I = theano.shared( np.random.normal(0, std, size=(self.num_items, self.factors) ).astype( self.floatX ), name='I' )
        self.S = theano.shared( np.random.normal(0, std, size=(self.num_items, self.factors) ).astype( self.floatX ), name='S' )
        
        self.IC = theano.shared( np.random.normal(0, std, size=(self.num_items, self.factors) ).astype( self.floatX ), name='I1' )

        self.BS = theano.shared( np.random.normal(0, std, size=(self.num_items,1) ).astype( self.floatX ), name='BS' )
        self.BI = theano.shared( np.random.normal(0, std, size=(self.num_items,1) ).astype( self.floatX ), name='BI' )
        
        self.F = theano.shared( np.random.normal(0, std, size=(self.num_items, 1) ).astype( self.floatX ), name='F' )
        
        self.hack_matrix = np.ones((self.batch, self.batch + self.samples), dtype=self.floatX)
        np.fill_diagonal(self.hack_matrix, 0)
        self.hack_matrix = theano.shared(self.hack_matrix, borrow=True)
        
        self._generate_train_model_batch_function()
        self._generate_predict_function()
        self._generate_predict_batch_function()
         
    
    def init_items(self, train):
        
        index_item = train.columns.get_loc( self.item_key )
                
        for row in train.itertuples(index=False):
            
            ci = row[index_item]
            
            if not ci in self.item_map: 
                self.item_map[ci] = self.item_count
                self.item_list[self.item_count] = ci
                self.item_count = self.item_count + 1                  
    
    def init_sessions(self, train):
        
        index_session = train.columns.get_loc( self.session_key )
        index_item = train.columns.get_loc( self.item_key )
        
        self.sessions = []
        self.session_map = {}
        
        train.sort_values( [self.session_key,self.time_key], inplace=True )
        
        prev_session = -1
        
        for row in train.itertuples(index=False):
            
            item = self.item_map[ row[index_item] ]
            session = row[index_session]
            
            if prev_session != session: 
                self.sessions.append(session)
                self.session_map[session] = []
            
            self.session_map[session].append(item)
            prev_session = session
    
    def _generate_train_model_batch_function(self):
        
        s = T.matrix('s', dtype=self.floatX)
        i = T.vector('i', dtype=self.intX)
        y = T.vector('y', dtype=self.intX)
        items = T.vector('items', dtype=self.intX)
        
        Sit = self.S[items]
        sit = s.T[items]
        
        Iy = self.I[y]
        BSy = self.BS[y]
        BIy = self.BI[y]
        
        ICi = self.IC[i]
        
        Fy = self.F[y]
        
        se = T.dot( Sit.T, sit )
        predS =  T.dot( Iy, se ).T + BSy.flatten()
        
        predI = T.dot( ICi, Iy.T ) + BIy.flatten()
        
        pred = Fy.flatten() * predS + (1-Fy.flatten()) * predI
        pred = getattr(self, self.activation )( pred )
        
        cost = getattr(self, self.objective )( pred, y )
        
        #updates = getattr(self, self.learn)(cost, [self.S,self.I,self.IC,self.BI,self.BS], self.learning_rate)
        updates = getattr(self, self.learn)(cost, [self.S,self.I,self.IC,self.BI,self.BS, self.F], [Sit,Iy,ICi,BIy,BSy],[items,y,i,y,y,y], self.learning_rate)
        
        self.train_model_batch = theano.function(inputs=[s, i, y, items], outputs=cost, updates=updates, profile=True  )
    
    def _generate_predict_function(self):
        
        s = T.vector('s', dtype=self.floatX)
        i = T.scalar('i', dtype=self.intX)
        
        se = T.dot( self.S.T, s.T )
        
        predS = T.dot( self.I, se ).T + self.BS.flatten()
        predI = T.dot( self.IC[i], self.I.T ) + self.BI.flatten()
        
        pred = self.F.flatten() * predS + (1-self.F.flatten()) * predI
        pred = getattr(self, self.activation )( pred )
        
        self.predict = theano.function(inputs=[s, i], outputs=pred )
    
    def _generate_predict_batch_function(self):
        
        s = T.matrix('s', dtype=self.floatX)
        i = T.vector('i', dtype=self.intX)
        
        se = T.dot( self.S.T, s.T )
        
        predS = T.dot( self.I, se ).T + self.BS
        predI = T.dot( self.IC[i], self.I.T ) + self.BI
        
        pred = predS + predI
        
        pred = getattr(self, self.activation )( pred )
        
        self.predict_batch = theano.function(inputs=[s, i], outputs=pred ) #, updates=updates )
        
        
    def bpr_old(self, predy, y ):
        ytrue = predy.diagonal()
        obj = T.sum( ( T.log( T.nnet.sigmoid( ytrue - predy.T ) ) 
                        - self.regularization * (self.S[y] ** 2).sum(axis=1)
                        - self.regularization * (self.I[y] ** 2).sum(axis=1)
                        - self.regularization * (self.IC[y] ** 2).sum(axis=1)
                        - self.regularization * (self.BI[y] ** 2)
                        - self.regularization * (self.BS[y] ** 2) ) ) 
        return -obj
    
    def bpr(self, pred_mat, y ):
        ytrue = pred_mat.T.diagonal()
        obj = -T.sum( T.log( T.nnet.sigmoid( ytrue - pred_mat ) ) )
        return obj
    
    def bpr_max(self, pred_mat, y):
        loss=0.5
        softmax_scores = self.softmax_neg(pred_mat.T).T
        return T.cast(T.mean(-T.log(T.sum(T.nnet.sigmoid(T.diag(pred_mat.T)-pred_mat)*softmax_scores, axis=0)+1e-24)+loss*T.sum((pred_mat**2)*softmax_scores, axis=0)), self.floatX)
    
    def bpr_max_org(self, pred_mat, y):
        softmax_scores = self.softmax_neg(pred_mat).T
        return T.cast(T.mean(-T.log(T.sum(T.nnet.sigmoid(T.diag(pred_mat)-pred_mat.T)*softmax_scores, axis=0)+1e-24)+self.regularization*T.sum((pred_mat.T**2)*softmax_scores, axis=0)), self.floatX)
    
    
    def bpr_mean(self, pred_mat, y ):
        ytrue = pred_mat.T.diagonal()
        obj = -T.mean( T.log( T.nnet.sigmoid( ytrue - pred_mat ) ) )
        return obj
    
    def top1(self, predy, y ):
        ytrue = predy.diagonal()
        obj = T.mean( T.log( T.nnet.sigmoid( -ytrue + predy.T ) ) 
                       - self.regularization * (self.S[y] ** 2).sum(axis=1)
                        - self.regularization * (self.I[y] ** 2).sum(axis=1)
                        - self.regularization * (self.IC[y] ** 2).sum(axis=1)
                        - self.regularization * (self.BI[y] ** 2)
                        - self.regularization * (self.BS[y] ** 2) )
        return obj
    
    def top1_2(self, predy, y ):
        predy = predy.T
        obj = T.mean( T.nnet.sigmoid(-T.diag(predy)+predy.T)+T.nnet.sigmoid(predy.T**2) )
        return obj
    
    def top1_max(self, yhat, y):
        yhatT = yhat.T
        softmax_scores = self.softmax_neg(yhatT)      
        tmp = softmax_scores.T*(T.nnet.sigmoid(-T.diag(yhatT)+yhat)+T.nnet.sigmoid(yhat**2))      
        return T.mean(T.sum(tmp, axis=0))
    
    def cross_entropy(self, pred_mat, y ):
        obj = T.mean( -T.log( pred_mat.diagonal() + 1e-24 ) )
        return obj
    
    
    def softmax_neg(self, X):
        if hasattr(self, 'hack_matrix'):
            X = X * self.hack_matrix
            e_x = T.exp(X - X.max(axis=1).dimshuffle(0, 'x')) * self.hack_matrix
        else:
            e_x = T.fill_diagonal(T.exp(X - X.max(axis=1).dimshuffle(0, 'x')), 0)
        return e_x / e_x.sum(axis=1).dimshuffle(0, 'x')
    
    
    def sgd(self, loss, param_list, learning_rate=0.01):
        
        all_grads = theano.grad(loss, param_list )
        
        updates = []
        
        for p, g in zip(param_list, all_grads):
            updates.append( (p, p - learning_rate * g ) )
        
        return updates
    
    
    def adam(self, loss, param_list, learning_rate=0.001, b1=0.9, b2=0.999, e=1e-8, gamma=1-1e-8):
        """
        ADAM update rules
        Default values are taken from [Kingma2014]
        References:
        [Kingma2014] Kingma, Diederik, and Jimmy Ba.
        "Adam: A Method for Stochastic Optimization."
        arXiv preprint arXiv:1412.6980 (2014).
        http://arxiv.org/pdf/1412.6980v4.pdf
        """
        
        updates = []
        all_grads = theano.grad(loss, param_list)
        alpha = learning_rate
        t = theano.shared(np.float32(1))
        b1_t = b1*gamma**(t-1)   #(Decay the first moment running average coefficient)
    
        for theta_previous, g in zip(param_list, all_grads):
            m_previous = theano.shared(np.zeros(theta_previous.get_value().shape,
                                                dtype=self.floatX))
            v_previous = theano.shared(np.zeros(theta_previous.get_value().shape,
                                                dtype=self.floatX))
    
            m = b1_t*m_previous + (1 - b1_t)*g                             # (Update biased first moment estimate)
            v = b2*v_previous + (1 - b2)*g**2                              # (Update biased second raw moment estimate)
            m_hat = m / (1-b1**t)                                          # (Compute bias-corrected first moment estimate)
            v_hat = v / (1-b2**t)                                          # (Compute bias-corrected second raw moment estimate)
            theta = theta_previous - (alpha * m_hat) / (T.sqrt(v_hat) + e) #(Update parameters)
    
            updates.append((m_previous, m))
            updates.append((v_previous, v))
            updates.append((theta_previous, theta) )
            
        updates.append((t, t + 1.))
        
        return updates
    
    def adagrad(self, loss, param_list, learning_rate=1.0, epsilon=1e-6):
        
        updates = []
        all_grads = theano.grad(loss, param_list)
        
        for param, grad in zip(param_list, all_grads):
            value = param.get_value( borrow=True )
            accu = theano.shared(np.zeros(value.shape, dtype=value.dtype),
                                 broadcastable=param.broadcastable)
            accu_new = accu + grad ** 2
            
            updates.append( ( accu, accu_new ) )
            updates.append( ( param, param - (learning_rate * grad / T.sqrt(accu_new + epsilon) ) ) )
            
        return updates
    
    def adagrad_sub(self, loss, param_list, subparam_list, idx, learning_rate=1.0, epsilon=1e-6 ):
        
        updates = []

        all_grads = theano.grad(loss, subparam_list)
        
        for i in range(len(all_grads)):
            
            grad = all_grads[i]
            param = param_list[i]
            index = idx[i]
            subparam = subparam_list[i]
            
            accu = theano.shared(param.get_value(borrow=False) * 0., borrow=True)
 
            accu_s = accu[index]
            accu_new = accu_s + grad ** 2
            updates.append( ( accu, T.set_subtensor(accu_s, accu_new) ) )
            updates.append( ( param, T.inc_subtensor(subparam, - (learning_rate * grad / T.sqrt(accu_new + epsilon) ) ) ) )
            
        return updates
    
    def linear(self, param):
        return param
    
    def sigmoid(self, param):
        return T.nnet.sigmoid( param )
    
    def uf_sigmoid(self, param):
        return T.nnet.ultra_fast_sigmoid( param )
    
    def hard_sigmoid(self, param):
        return T.nnet.hard_sigmoid( param )
    
    def relu(self, param):
        return T.nnet.relu( param )
    
    def softmax(self, param):
        return T.nnet.softmax( param )
    
    def softsign(self, param):
        return T.nnet.softsign( param )
    
    def softplus(self, param):
        return T.nnet.softplus( param )
    
    def tanh(self, param):
        return T.tanh( param )
     
    def predict_next( self, session_id, input_item_id, predict_for_item_ids, skip=False, type='view', timestamp=0 ):
        '''
        Gives predicton scores for a selected set of items on how likely they be the next item in the session.
                
        Parameters
        --------
        session_id : int or string
            The session IDs of the event.
        input_item_id : int or string
            The item ID of the event. Must be in the set of item IDs of the training set.
        predict_for_item_ids : 1D array
            IDs of items for which the network should give prediction scores. Every ID must be in the set of item IDs of the training set.
            
        Returns
        --------
        out : pandas.Series
            Prediction scores for selected items on how likely to be the next item of this session. Indexed by the item IDs.
        
        '''
                
        if( self.session != session_id ): #new session      
                
            self.session = session_id
            self.session_items = np.zeros(self.num_items, dtype=np.float32)
            self.session_count = 0
        
        if type == 'view':
            self.session_count += 1
            self.session_items[ self.item_map[input_item_id] ] = self.session_count
        
        if skip:
            return
         
        predictions = self.predict( self.session_items, self.item_map[input_item_id] )# / self.session_count, self.item_map[input_item_id] )
        series = pd.Series(data=predictions, index=self.item_list)
        series = series[predict_for_item_ids]
                
        return series 
    
    