#!/usr/bin/env python
import torch
import torch.multiprocessing as mp
import random
import numpy as np
from collections import namedtuple
from duelling_network import DuellingDQN
from env import make_local_env
import cv2

Transition = namedtuple('Transition', ['S', 'A', 'R', 'Gamma', 'q'])
N_Step_Transition = namedtuple('N_Step_Transition', ['S_t', 'A_t', 'R_ttpB', 'Gamma_ttpB', 'qS_t', 'S_tpn', 'qS_tpn', 'key'])


class ExperienceBuffer(object):
    def __init__(self, n, actor_id):
        """
        Implements a circular/ring buffer to store n-step transition data used by the actor
        :param n:
        """
        self.local_1step_buffer = list()  #  To store single step transitions to compose n-step transitions
        self.local_nstep_buffer= list()  #  To store n-step transitions b4 they r batched, prioritized and sent to replay mem
        self.idx = -1
        self.capacity = n
        self.gamma = 0.99
        self.id = actor_id
        self.n_step_seq_num = 0  # Used to compose the unique key per per-actor and per n-step transition stored

    def update_buffer(self):
        """
        Updates the accumulated per-step discount and the partial return for every item in the buffer. This should be
        called after every new transition is added to the buffer
        :return: None
        """
        for i in range(self.B - 1):
            R = self.local_1step_buffer[i].R
            Gamma = 1
            for k in range(i + 1, self.B ):
                Gamma *= self.gamma
                R += Gamma * self.local_1step_buffer[k].R
            self.local_1step_buffer[i] = Transition(self.local_1step_buffer[i].S,
                                                    self.local_1step_buffer[i].A, R, Gamma,
                                                    self.local_1step_buffer[i].q)
    def construct_nstep_transition(self, data):
        if self.idx == -1:  #  Episode ended at the very first step in this n-step transition
            return
        key = str(self.id) + str(self.n_step_seq_num)
        n_step_transition = N_Step_Transition(*self.local_1step_buffer[0], data.S, data.q, key)
        self.n_step_seq_num += 1
        #  Put the n_step_transition into a local memory store
        self.local_nstep_buffer.append(n_step_transition)
        #  Free-up the buffer
        self.local_1step_buffer.clear()
        #  Reset the memory index
        self.idx = -1

    def add(self, data):
        """
        Add transition data to the Experience Buffer and calls update_buffer
        :param data: tuple containing a transition data of type Transition(s, a, r, gamma, q)
        :return: None
        """
        if self.idx  + 1 < self.capacity:
            self.idx += 1
            self.local_1step_buffer.append(None)
            self.local_1step_buffer[self.idx] = data
            self.update_buffer()  #  calculate the accumulated per-step disc & partial return for all entries
        else:  # single-step buffer has reached its capacity, n. Compute
            #  Construct the n-step transition
            self.construct_nstep_transition(data)


    def get(self, batch_size):
        assert batch_size <= self.size, "Requested n-step transitions batch size is more than available"
        batch_of_n_step_transitions = self.local_nstep_buffer[: batch_size]
        del self.local_nstep_buffer[: batch_size]
        return batch_of_n_step_transitions

    @property
    def B(self):
        """
        The current size of local single step buffer. B follows the same notation as in the Ape-X paper(TODO: insert link to paper)
        :return: The current size of the buffer
        """
        return len(self.local_1step_buffer)

    @property
    def size(self):
        """
        The current size of the local n-step experience memory
        :return:
        """
        return len(self.local_nstep_buffer)


class Actor(mp.Process):
    def __init__(self, actor_id, env_conf, shared_state, shared_replay_mem, actor_params):
        super(Actor, self).__init__()
        self.actor_id = actor_id  # Used to compose a unique key for the transitions generated by each actor
        state_shape = tuple(env_conf['state_shape'])
        action_dim = env_conf['action_dim']
        self.params = actor_params
        self.shared_state = shared_state
        self.T = self.params["T"]
        self.Q = DuellingDQN(state_shape, action_dim)
        self.Q.load_state_dict(shared_state["Q_state_dict"])
        self.env = make_local_env(env_conf['name'])
        self.policy = self.epsilon_greedy_Q
        self.local_experience_buffer = ExperienceBuffer(self.params["num_steps"], self.actor_id)
        self.global_replay_queue = shared_replay_mem
        eps = self.params['epsilon']
        N = self.params['num_actors']
        alpha = self.params['alpha']
        self.epsilon = eps**(1 + alpha * self.actor_id / (N-1))
        self.gamma = self.params['gamma']
        self.num_buffered_steps = 0  # Used to compose a unique key for the transitions generated by each actor
        self.rgb2gray = lambda x: np.dot(x, np.array([[0.299, 0.587, 0.114]]).T)  # RGB to Gray scale
        self.torch_shape = lambda x: np.reshape(self.rgb2gray(x), (1, x.shape[1], x.shape[0]))  # WxHxC to CxWxH
        self.obs_preproc = lambda x: np.resize(self.torch_shape(x), state_shape)

    def epsilon_greedy_Q(self, qS_t):
        if random.random() >= self.epsilon:
            return np.argmax(qS_t)
        else:
            return random.choice(list(range(len(qS_t))))

    def compute_priorities(self, n_step_transitions):
        n_step_transitions = N_Step_Transition(*zip(*n_step_transitions))
        # Convert tuple to numpy array
        rew_t_to_tpB = np.array(n_step_transitions.R_ttpB)
        gamma_t_to_tpB = np.array(n_step_transitions.Gamma_ttpB)
        qS_tpn = np.array(n_step_transitions.qS_tpn)
        A_t = np.array(n_step_transitions.A_t, dtype=np.int)
        qS_t = np.array(n_step_transitions.qS_t)

        #print("np.max(qS_tpn,1):", np.max(qS_tpn, 1))
        #  Calculate the absolute n-step TD errors
        n_step_td_target =  rew_t_to_tpB + gamma_t_to_tpB * np.max(qS_tpn, 1)
        #print("td_target:", n_step_td_target)
        n_step_td_error = n_step_td_target - np.array([ qS_t[i, A_t[i]] for i in range(A_t.shape[0])])
        #print("td_err:", n_step_td_error)
        priorities = {k: val for k in n_step_transitions.key for val in abs(n_step_td_error) }
        return priorities


    def run(self):
        """
        A method to gather experiences using the Actor's policy and the Actor's environment instance.
          - Periodically syncs the parameters of the Q network used by the Actor with the latest Q parameters made available by
            the Learner process.
          - Stores the single step transitions and the n-step transitions in a local experience buffer
          - Periodically flushes the n-step transition experiences to the global replay queue
        :param T: The total number of time steps to gather experience
        :return:
        """
        # 3. Get initial state from environment
        obs = self.obs_preproc(self.env.reset())
        ep_reward = []
        for t in range(self.T):
            with torch.no_grad():
                qS_t = self.Q(torch.from_numpy(obs).unsqueeze(0).float())[2].squeeze().numpy()
            # 5. Select the action using the current policy
            action = self.policy(qS_t)
            # 6. Apply action in the environment
            next_obs, reward, done, _ = self.env.step(action)
            # 7. Add data to local buffer
            self.local_experience_buffer.add(Transition(obs, action, reward , self.gamma, qS_t))
            obs = self.obs_preproc(next_obs)
            ep_reward.append(reward)
            print("Actor#", self.actor_id, "t=", t, "action=", action, "reward:", reward, "1stp_buf_size:", self.local_experience_buffer.B, end='\r')

            if done:  # Not mentioned in the paper's algorithm
                # Truncate the n-step transition as the episode has ended; NOTE: Reward is set to 0
                self.local_experience_buffer.construct_nstep_transition(Transition(obs, action, 0, self.gamma, qS_t))
                # Reset the environment
                obs = self.obs_preproc(self.env.reset())
                print("Actor#:", self.actor_id, "t:", t, "  ep_len:", len(ep_reward), "  ep_reward:", np.sum(ep_reward))
                ep_reward = []

            # 8. Periodically send data to replay
            if self.local_experience_buffer.size >= self.params['n_step_transition_batch_size']:
                # 9. Get batches of multi-step transitions
                n_step_experience_batch = self.local_experience_buffer.get(self.params['n_step_transition_batch_size'])
                # 10.Calculate the priorities for experience
                priorities = self.compute_priorities(n_step_experience_batch)
                # 11. Send the experience to the global replay memory
                self.global_replay_queue.put([priorities, n_step_experience_batch])

            if t % self.params['Q_network_sync_freq'] == 0:
                # 13. Obtain latest network parameters
                self.Q.load_state_dict(self.shared_state["Q_state_dict"])

if __name__ == "__main__":
    """ 
    Simple standalone test routine for Actor class
    """
    env_conf = {"state_shape": (1, 84, 84),
                "action_dim": 4,
                "name": "Breakout-v0"}
    params= {"local_experience_buffer_capacity": 10,
             "epsilon": 0.4,
             "alpha": 7,
             "gamma": 0.99,
             "num_actors": 2,
             "n_step_transition_batch_size": 5,
             "Q_network_sync_freq": 10,
             "num_steps": 3,
             "T": 101 # Total number of time steps to gather experience

             }
    dummy_q = DuellingDQN(env_conf['state_shape'], env_conf['action_dim'])
    mp_manager = mp.Manager()
    shared_state = mp_manager.dict()
    shared_state["Q_state_dict"] = dummy_q.state_dict()
    shared_replay_mem = mp_manager.Queue()
    actor = Actor(1, env_conf, shared_state, shared_replay_mem, params)
    actor.run()
    print("Main: replay_mem.size:", shared_replay_mem.qsize())
    for i in range(shared_replay_mem.qsize()):
        p, xp_batch = shared_replay_mem.get()
        print("priority:", p)
