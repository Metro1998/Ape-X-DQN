#!/usr/bin/env python
import torch
import time
import numpy as np
from collections import namedtuple
from duelling_network import DuellingDQN

N_Step_Transition = namedtuple('N_Step_Transition', ['S_t', 'A_t', 'R_ttpB', 'Gamma_ttpB', 'qS_t', 'S_tpn', 'qS_tpn', 'key'])

class Learner(object):
    def __init__(self, env_conf, learner_params, shared_state, shared_replay_memory):
        state_shape = env_conf['state_shape']
        action_dim = env_conf['action_dim']
        self.params = learner_params
        self.shared_state = shared_state
        self.Q = DuellingDQN(state_shape, action_dim)
        self.Q_double = DuellingDQN(state_shape, action_dim)  # Target Q network which is slow moving replica of self.Q
        if self.params['load_saved_state']:
            try:
                saved_state = torch.load(self.params['load_saved_state'])
                self.Q.load_state_dict(saved_state['Q_state'])
            except FileNotFoundError:
                print("WARNING: No trained model found. Training from scratch")
        self.shared_state["Q_state_dict"] = self.Q.state_dict()
        self.replay_memory = shared_replay_memory
        self.optimizer = torch.optim.RMSprop(self.Q.parameters(), lr=0.00025 / 4, weight_decay=0.95, eps=1.5e-7)
        self.num_q_updates = 0

    def compute_loss_and_update_priorities(self, xp_batch):
        n_step_transitions = N_Step_Transition(*zip(*xp_batch))
        # Convert tuple to numpy array
        S_t = np.array(n_step_transitions.St)
        S_tpn = np.array(n_step_transitions.S_tpn)
        rew_t_to_tpB = np.array(n_step_transitions.R_ttpB)
        gamma_t_to_tpB = np.array(n_step_transitions.Gamma_ttpB)
        A_t = np.array(n_step_transitions.A_t, dtype=np.int)

        G_t = rew_t_to_tpB + gamma_t_to_tpB * self.Q_double(S_tpn, torch.argmax(self.Q(S_tpn)))
        Q_S_A = self.Q(S_t).gather(A_t, 1)
        batch_td_error = G_t - Q_S_A
        loss = 1/2 * (batch_td_error)**2
        # Update the priorities of the experience
        priorities = {k: v for k in xp_batch.keys for v in abs(batch_td_error)}
        self.replay_memory.set_priorities(priorities)

        return loss

    def update_Q(self, loss):
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()
        self.num_q_updates += 1

        if self.num_q_updates % self.params['q_target_sync_freq']:
            self.Q_double.load_state_dict(self.Q.state_dict())

    def learn(self, T):
        while self.replay_memory.size() <  50000:
            time.sleep(1)
        for t in range(T):
            id, prioritized_xp_batch = self.replay_memory.sample(self.params['replay_sample_size'])
            loss = self.compute_loss_and_update_priorities(prioritized_xp_batch)
            self.update_Q(loss)
            self.shared_state['Q_state_dict'] = self.Q.state_dict()
            priorities = self.compute_priorities(prioritized_xp_batch)
            self.replay_memory.set_priority(id, priorities)

            if t % self.params['remove_old_xp_freq'] == 0:
                self.replay_memory.cleanup_old_xp()
