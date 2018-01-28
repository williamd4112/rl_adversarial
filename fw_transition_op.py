import numpy as np
import numpy.random as npr
import time
import math
import theano
import lasagne
from rllab.policies.gaussian_mlp_policy import GaussianMLPPolicy
from rllab.envs.env_spec import EnvSpec
from rllab.spaces import Box
import theano.tensor as TT
#import lasagne.nonlinearities as NL

#inputSize, outputSize, env, v, lr, batchsize,
#v['which_agent'], x_index, y_index, fwd_obs, policy_dist['mean'], print_minimal, policy_dist["log_std"]

class Fw_Trans_Model:

    def __init__(self, inputSize, outputSize, env, v, learning_rate, batchsize, which_agent, x_index, y_index,
                fwd_obs, policy, print_minimal):


        #init vars
        #self.sess = sess
        self.batchsize = batchsize
        self.which_agent = which_agent
        self.x_index = x_index
        self.y_index = y_index
        self.inputSize = inputSize
        self.outputSize = outputSize
        self.print_minimal = print_minimal

        LOW = -1000000
        HIGH = 1000000
        self.act_dim = env.spec.action_space.flat_dim
        self.obs_dim = env.spec.observation_space.flat_dim
        #obs_to_act_spec = env.spec
        obsact_to_obs_spec = EnvSpec(observation_space=Box(LOW, HIGH, shape=(self.act_dim + self.obs_dim,)),
                                            action_space=Box(LOW, HIGH, shape=(self.obs_dim,)))

        #TODO: Think, whether to learn std for backwards policy or not.
        #self.bw_act_pol = GaussianMLPPolicy(
        # env_spec=obs_to_act_spec,
        # hidden_sizes=(64, 64),
        # learn_std=v['bw_variance_learn'],
        #)

        self.bw_obs_pol = GaussianMLPPolicy(
         env_spec=obsact_to_obs_spec,
         hidden_sizes=(v['bw_model_hidden_size'], v['bw_model_hidden_size']),
         learn_std=v['bw_variance_learn']
         #hidden_nonlinearity=NL.rectify
         )

        self.obs_in = fwd_obs#TT.matrix('obs_in')
        self.actual_state = TT.matrix('actual_state')
        policy_dist = policy.dist_info_sym(self.obs_in)
        self.act_out = policy_dist['mean'] + 0.0 * policy_dist['log_std'] #TT.matrix('act_out')
        self.obsact_in = TT.concatenate([self.obs_in, self.act_out], axis=1)
        self.diff_out = TT.matrix('diff_out')

        fw_learning_rate = v['bw_learning_rate']
        self.bw_obs_dist = self.bw_obs_pol.dist_info_sym(self.obsact_in)
        bw_obs_loss = -TT.sum(self.bw_obs_pol.distribution.log_likelihood_sym(self.diff_out, self.bw_obs_dist))

        bw_obs_params = self.bw_obs_pol.get_params_internal()
        bw_sa_to_s_update = lasagne.updates.adam(bw_obs_loss, bw_obs_params,
                        learning_rate=fw_learning_rate)

        self.bw_obs_train = theano.function([self.obsact_in, self.diff_out], bw_obs_loss,
                        updates=bw_sa_to_s_update, allow_input_downcast=True)


        self.fw_dynamics_dist = self.bw_obs_pol.dist_info_sym(self.obsact_in)
        fw_loss = -TT.sum(policy.distribution.log_likelihood_sym(self.actual_state - self.obs_in, self.fw_dynamics_dist))
        #self.prediction_loss = TT.sum((self.fw_dynamics_dist['mean'] - self.actual_state)**2)
        fw_update = lasagne.updates.adam(fw_loss, bw_obs_params + policy.get_params_internal(), learning_rate=fw_learning_rate)
        self.fw_pred_func = theano.function([self.obs_in, self.actual_state], fw_loss, updates=fw_update, allow_input_downcast=True)


    def train(self, dataX, dataZ, dataX_new, dataZ_new, nEpoch, save_dir, fraction_use_new):

        #init vars
        start = time.time()
        training_loss_list = []
        nData_old = dataX.shape[0]
        num_new_pts = dataX_new.shape[0]
        #obs_shape = dataZ.shape[1]

        #how much of new data to use per batch
        if(num_new_pts<(self.batchsize*fraction_use_new)):
            batchsize_new_pts = num_new_pts #use all of the new ones
        else:
            batchsize_new_pts = int(self.batchsize*fraction_use_new)

        #how much of old data to use per batch
        batchsize_old_pts = int(self.batchsize- batchsize_new_pts)

        #training loop
        for i in range(nEpoch):

            #reset to 0
            avg_loss=0
            num_batches=0

            if(batchsize_old_pts>0):
                print("nothing is going on")

            #train completely from new set
            else:
                for batch in range(int(math.floor(num_new_pts / batchsize_new_pts))):

                    #walk through the shuffled new data
                    dataX_batch = dataX_new[batch*batchsize_new_pts:(batch+1)*batchsize_new_pts, :]
                    dataZ_batch = dataZ_new[batch*batchsize_new_pts:(batch+1)*batchsize_new_pts, :]

                    #data_x = dataX_batch[:,0:obs_shape]
                    #data_y = dataX_batch[:, obs_shape:]
                    #loss = self.bw_act_train(data_x, data_y)
                    bw_obs_losses = self.bw_obs_train(dataX_batch, dataZ_batch)

                    #training_loss_list.append(loss)
                    avg_loss+= bw_obs_losses#[0]
                    num_batches+=1

                #shuffle new dataset after an epoch (if training only on it)
                p = npr.permutation(dataX_new.shape[0])
                dataX_new = dataX_new[p]
                dataZ_new = dataZ_new[p]

            #save losses after an epoch
            np.save(save_dir + '/training_losses.npy', training_loss_list)
            if(not(self.print_minimal)):
                if((i%10)==0):
                    print("\n=== Epoch {} ===".format(i))
                    print ("loss: ", avg_loss/num_batches)

        if(not(self.print_minimal)):
            print ("Training set size: ", (nData_old + dataX_new.shape[0]))
            print("Training duration: {:0.2f} s".format(time.time()-start))


        #done
        return (avg_loss/num_batches)#, old_loss, new_loss


    #multistep prediction using the learned dynamics model at each step
    def do_forward_sim(self, forwardsim_x_true, num_step, many_in_parallel, env_inp, which_agent, mean_x, mean_y, mean_z, std_x, std_y, std_z):

        #init vars
        state_list = []
        action_list = []
        if(many_in_parallel):
            #init vars
            print("Future work..")
        else:
            curr_state = np.copy(forwardsim_x_true) #curr state is of dim NN input
            for i in range(num_step):
                curr_state_preprocessed = curr_state - mean_x
                curr_state_preprocessed = np.nan_to_num(curr_state_preprocessed/std_x)
                action = self.bw_act_pol.get_action(curr_state_preprocessed)[0]
                action_ = action * std_y + mean_y
                state_difference = self.bw_obs_pol.get_action(np.concatenate((curr_state_preprocessed, action)))[0]
                state_differences= (state_difference*std_z)+mean_z
                next_state = curr_state + state_differences
                #copy the state info
                curr_state= np.copy(next_state)
                state_list.append(np.copy(curr_state))
                action_list.append(np.copy(action_))

        return state_list, action_list