"""
Code for training RBMs with contrastive divergence. Tries to be as
quick and memory-efficient as possible while utilizing only pure Python
and NumPy.
"""

# Copyright (c) 2009, David Warde-Farley
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
# 3. The name of the author may not be used to endorse or promote products
#    derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES
# OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
# IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT
# NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF
# THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import sys
import time
import random
import matplotlib
# matplotlib.use('Agg') # uncomment this if you are using the VM, gives text
                        # ouptut only
# matplotlib.use('TkAgg') # uncomment this if you are using installed version
                          #gives visual output
import matplotlib.pyplot as plt
import numpy as np
from data_utils import load_data_mnist
import os

class RBM(object):
    """
    Class representing a basic restricted Boltzmann machine, with
    binary stochastic visible units and binary stochastic hidden
    units.
    """
    def __init__(self, nvis, nhid, mfvis=True, mfhid=False, initvar=0.1):
        nweights = nvis * nhid
        vb_offset = nweights
        hb_offset = nweights + nvis

        # One parameter matrix, with views onto it specified below.
        self.params = np.empty((nweights + nvis + nhid))

        # Weights between the hiddens and visibles
        self.weights = self.params[:vb_offset].reshape(nvis, nhid)

        # Biases on the visible units
        self.visbias = self.params[vb_offset:hb_offset]

        # Biases on the hidden units
        self.hidbias = self.params[hb_offset:]

        # Attributes for scratch arrays used during sampling.
        self._hid_states = None
        self._vis_states = None

        # Instance-specific mean field settings.
        self._mfvis = mfvis
        self._mfhid = mfhid

    @property
    def numvis(self):
        """The number of visible units (i.e. dimension of the input)."""
        return self.visbias.shape[0]

    @property
    def numhid(self):
        """The number of hidden units in this model."""
        return self.hidbias.shape[0]

    def _prepare_buffer(self, ncases, kind):
        """
        Prepare the _hid_states and _vis_states buffers for
        use for a minibatch of size `ncases`, reshaping or
        reallocating as necessary. `kind` is one of 'hid', 'vis'.
        """
        if kind not in ['hid', 'vis']:
            raise ValueError('kind argument must be hid or vis')
        name = '_%s_states' % kind
        num = getattr(self, 'num%s' % kind)
        buf = getattr(self, name)
        if buf is None or buf.shape[0] < ncases:
            if buf is not None:
                del buf
            buf = np.empty((ncases, num))
            setattr(self, name, buf)
        buf[...] = np.NaN
        return buf[:ncases]

    def hid_activate(self, input, mf=False):
        """
        Activate the hidden units by sampling from their conditional
        distribution given each of the rows of `inputs. If `mf` is True,
        return the deterministic, real-valued probabilities of activation
        in place of stochastic binary samples ('mean-field').
        """
        input = np.atleast_2d(input)
        ncases, ndim = input.shape
        hid = self._prepare_buffer(ncases, 'hid')
        self._update_hidden(input, hid, mf)
        return hid

    def _update_hidden(self, vis, hid, mf=False):
        """
        Update hidden units by writing new values to array `hid`.
        If `mf` is False, hidden unit values are sampled from their
        conditional distribution given the visible unit configurations
        specified in each row of `vis`. If `mf` is True, the
        deterministic, real-valued probabilities of activation are
        written instead of stochastic binary samples ('mean-field').
        """
        hid[...] = np.dot(vis, self.weights)
        hid[...] += self.hidbias
        hid *= -1.
        np.exp(hid, hid)
        hid += 1.
        hid **= -1.
        if not mf:
            self.sample_hid(hid)

    def _update_visible(self, vis, hid, mf=False):
        """
        Update visible units by writing new values to array `hid`.
        If `mf` is False, visible unit values are sampled from their
        conditional distribution given the hidden unit configurations
        specified in each row of `hid`. If `mf` is True, the
        deterministic, real-valued probabilities of activation are
        written instead of stochastic binary samples ('mean-field').
        """

        # Implements 1/(1 + exp(-WX) with in-place operations
        vis[...] = np.dot(hid, self.weights.T)
        vis[...] += self.visbias
        vis *= -1.
        np.exp(vis, vis)
        vis += 1.
        vis **= -1.
        if not mf:
           self.sample_vis(vis)

    @classmethod
    def binary_threshold(cls, probs):
        """
        Given a set of real-valued activation probabilities,
        sample binary values with the given Bernoulli parameter,
        and update the array in-placewith the Bernoulli samples.
        """
        samples = np.random.uniform(size=probs.shape)

        # Simulate Bernoulli trials with p = probs[i,j] by generating random
        # uniform and counting any number less than probs[i,j] as success.
        probs[samples < probs] = 1.

        # Anything not set to 1 should be 0 once floored.
        np.floor(probs, probs)

    # Binary hidden units
    sample_hid = binary_threshold

    # Binary visible units
    sample_vis = binary_threshold

    def gibbs_walk(self, nsteps, hid):
        """
        Perform nsteps of alternating Gibbs sampling,
        sampling the hidden units in parallel followed by the
        visible units.

        Depending on instantiation arguments, one or both sets of
        units may instead have "mean-field" activities computed.
        Mean-field is always used in lieu of sampling for the
        terminal hidden unit configuration.
        """
        hid = np.atleast_2d(hid)
        ncases = hid.shape[0]

        # Allocate (or reuse) a buffer with which to store
        # the states of the visible units
        vis = self._prepare_buffer(ncases, 'vis')

        for iter in xrange(nsteps):

            # Update the visible units conditioning on the hidden units.
            self._update_visible(vis, hid, self._mfvis)

            # Always do mean-field on the last hidden unit update to get a
            # less noisy estimate of the negative phase correlations.
            if iter < nsteps - 1:
                mfhid = self._mfhid
            else:
                mfhid = True

            # Update the hidden units conditioning on the visible units.
            self._update_hidden(vis, hid, mfhid)

        return self._vis_states[:ncases], self._hid_states[:ncases]

class GaussianBinaryRBM(RBM):
    def _update_visible(self, vis, hid, mf=False):
        vis[...] = np.dot(hid, self.weights.T)
        vis += self.visbias
        if not mf:
            self.sample_vis(vis)

    @classmethod
    def sample_vis(self, vis):
        vis += np.random.normal(size=vis.shape)

class CDTrainer(object):
    """An object that trains a model using vanilla contrastive divergence."""

    def __init__(self, model, weightcost=0.0002, rates=(1e-4, 1e-4, 1e-4),
                 cachebatchsums=True):
        self._model = model
        self._visbias_rate, self._hidbias_rate, self._weight_rate = rates
        self._weightcost = weightcost
        self._cachebatchsums = cachebatchsums
        self._weightstep = np.zeros(model.weights.shape)

    def train(self, data, epochs, cdsteps=1, minibatch=50, momentum=0.9):
        """
        Train an RBM with contrastive divergence, using `nsteps`
        steps of alternating Gibbs sampling to draw the negative phase
        samples.
        """
        data = np.atleast_2d(data)
        ncases, ndim = data.shape
        model = self._model

        if self._cachebatchsums:
            batchsums = {}

        mse = np.zeros(epochs)
        col = np.array([np.random.rand(),np.random.rand(),np.random.rand()])

        for epoch in xrange(epochs):

            # An epoch is a single pass through the training data.

            epoch_start = time.clock()

            # Mean squared error isn't really the right thing to measure
            # for RBMs with binary visible units, but gives a good enough
            # indication of whether things are moving in the right way.

            # mse = 0

            # Compute the summed visible activities once
            # ctr = 0
            for offset in xrange(0, ncases, minibatch):

                # Select a minibatch of data.
                batch = data[offset:(offset+minibatch)]

                batchsize = batch.shape[0]

                # Mean field pass on the hidden units f
                hid = model.hid_activate(batch, mf=True)

                # Correlations between the data and the hidden unit activations
                poscorr = np.dot(batch.T, hid)

                # Activities of the hidden units
                posact = hid.sum(axis=0)

                # Threshold the hidden units so that they can't convey
                # more than 1 bit of information in the subsequent
                # sampling (assuming the hidden units are binary,
                # which they most often are).
                model.sample_hid(hid)

                # Simulate Gibbs sampling for a given number of steps.
                vis, hid = model.gibbs_walk(cdsteps, hid)

                # Update the weights with the difference in correlations
                # between the positive and negative phases.

                thisweightstep = poscorr
                thisweightstep -= np.dot(vis.T, hid)
                thisweightstep /= batchsize
                thisweightstep -= self._weightcost * model.weights
                thisweightstep *= self._weight_rate

                self._weightstep *= momentum
                self._weightstep += thisweightstep

                model.weights += self._weightstep

                # The gradient of the visible biases is the difference in
                # summed visible activities for the minibatch.
                if self._cachebatchsums:
                    if offset not in batchsums:
                        batchsum = batch.sum(axis=0)
                        batchsums[offset] = batchsum
                    else:
                        batchsum = batchsums[offset]
                else:
                    batchsum = batch.sum(axis=0)

                visbias_step = batchsum - vis.sum(axis=0)
                visbias_step *= self._visbias_rate / batchsize

                model.visbias += visbias_step

                # The gradient of the hidden biases is the difference in
                # summed hidden activities for the minibatch.

                hidbias_step = posact - hid.sum(axis=0)
                hidbias_step *= self._hidbias_rate / batchsize

                model.hidbias += hidbias_step

                # Compute the squared error in-place.
                vis -= batch
                vis **= 2.

                # Add to the total epoch estimate.
                # mse += vis.sum() / ncases
                mse[epoch] += vis.sum() / ncases

            # Saving and displaying weights for the first layer
            if model.weights.shape[0]==784 and epoch%1==0:
                self.plot_rf(model.weights)

            plt.figure(1)
            plt.plot(mse[:epoch+1], color=col)
            plt.xlabel('Number of epochs')
            plt.ylabel('MSE')
            path_to_save = os.path.join(os.path.curdir, 'Result',
                                        'rbm_multiple_layers')
            if not os.path.exists(path_to_save):
                os.makedirs(path_to_save)
            plt.savefig(os.path.join(path_to_save,'rbm_plot.png'))
            plt.draw()

            print "Done epoch %d: %f seconds, MSE=%f" % \
                    (epoch + 1, time.clock() - epoch_start, mse[epoch])
            sys.stdout.flush()

    def plot_rf(self,w1,lim=1.0):
        w = w1.T

        N1 = int(np.sqrt(w.shape[1]))   # sqrt(784) = 28, you have 28x28 RFs
        N2 = int(np.ceil(np.sqrt(w.shape[0])))  # sqrt(256) = 16, you have 16x16 output cells

        W = np.zeros((N1*N2,N1*N2))             # You are creating a weight wall of 16x16 RF blocks

        for j in range(w.shape[0]):
            r = int(j/N2)
            c = int(j%N2)
            x = c*N1
            y = r*N1
            W[y:y+N1, x:x+N1] = w[j, :].reshape((N1, N1))

        plt.figure(2)
        plt.title('Weights between first and second layer')
        plt.imshow(W, vmin=-lim, vmax=lim)
        path_to_save = os.path.join(os.path.curdir, 'Result',
                                    'rbm_multiple_layers')
        if not os.path.exists(path_to_save):
            os.makedirs(path_to_save)
        plt.savefig(os.path.join(path_to_save, 'rf_fields.png'))
        plt.show()


def reconstruct(ipData,rbm_stack):
    """
    This function takes an input image and returns a reconstructed image using the RBM weights
    """
    x1 = ipData
    # obtaining the feature vector representing the input image
    for rbm in rbm_stack:
        W1 = rbm.weights
        H1 = rbm.hidbias
        x1 = np.array((np.matrix(W1.T)*np.matrix(x1.T)).T) + H1
        # sigmoid activation function
        x1 = 1/(1+np.exp(-x1))

    # reconstruct the image from the feature vector by tracing the rbm layers in the reverse direction
    for rbm in reversed(rbm_stack):
        W1 = rbm.weights
        V1 = rbm.visbias
        x1 = np.array((np.matrix(W1)*np.matrix(x1.T)).T) + V1
        # sigmoid activation function
        x1 = 1/(1+np.exp(-x1))

    return x1

def get_data(data1, target1, nClasses, nS):
    """
    This function gives a stack of randomly chosen and shuffled input image samples used for training
    nClasses = number of classes(digits starting from 0) used for training the RBM layers
    nS = number of samples used for training per class
    """
    ipDim = data1.shape[1]

    data = np.zeros((nClasses*nS,ipDim))

    for i in range(nClasses):
        idx1 = [i1 for i1, x in enumerate(target1) if x == i]
        idx = random.sample(idx1,nS)
        data[i*nS:(i+1)*nS,:] = data1[idx,:]

    np.random.shuffle(data)
    return data

"""
- The code trains a specified number of RBM layers.
- The numbmer of layers and number of nodes in each layers can be changed by the user.
- While training, a figure is displayed which shows the change in the Mean Square Error (MSE) between the training samples and the reconstructed samples vs number of epochs.
- At the end of training, a few examples of the input image and the reconstructed image are displayed.
- In addition to changing the architecture of the network, the number of epochs and the number of training samples can also be changed.
"""

if __name__=='__main__':
    data1, target1 = load_data_mnist()
    split = 60000
    opDim = 32 # dimension of output feature vector
    ipDim = data1.shape[1] # size of each input image
    nClasses = np.unique(target1).size # This should be less than or equal to 10 - MNIST dataset
    nS = 5000 # no. of samples per class for training
    nNodes = [ipDim, 256, 64, opDim] # No. of nodes in each layer of the network in the specified order
    nEpochs = 50 # Maximum number of epochs for training each layer

    if nClasses<=0:
        print 'Number of classes should be a positive number. Check nClasses in the code.'
        quit()

    train_data = get_data(data1[:split],target1[:split],nClasses,nS) # Get training data
    rbm_stack = [] # list of rbm layers

    # training the RBM layers one by one
    plt.ion()
    plt.show()
    data0 = train_data
    for i in range(len(nNodes)-1):
        rbm1 = RBM(nNodes[i], nNodes[i+1])
        rbm1.params[:] = np.random.uniform(-1./20, 1./20, len(rbm1.params))
        trainer = CDTrainer(rbm1)
        trainer.train(data0, nEpochs, minibatch=100)
        W1 = rbm1.weights
        V1 = rbm1.visbias
        H1 = rbm1.hidbias
        data0 = np.array((np.matrix(W1.T)*np.matrix(data0.T)).T) + H1

        # sigmoid activation function
        data0 = 1/(1+np.exp(-data0))

        rbm_stack.append(rbm1)

    # Get testing data
    nExamples = 20 # No. of examples to be displayed in the end
    test_data = get_data(data1[split:],target1[split:],nClasses,nExamples)
    n = int(np.ceil(np.sqrt(test_data.shape[1])))

    # Display the reconstruction output for some sample inputs
    plt.figure()
    A = plt.subplot(121,title='Input data')
    B = plt.subplot(122,title='Reconstructed data')
    for i in range(nExamples):
        ipData = test_data[i,:]
        opData = reconstruct(np.reshape(ipData,(1,test_data.shape[1])),rbm_stack)
        I = np.reshape(ipData,(n,n))
        O = np.reshape(opData,(n,n))

        A.imshow(I,cmap='Greys_r')
        B.imshow(O,cmap='Greys_r')
        plt.draw()
        time.sleep(2)

    plt.close('all')
