from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import logging
import abc
import tensorflow as tf
import numpy as np
from src import cluster
from queue import PriorityQueue
from src import definitions as defs
from tf_agents.environments import py_environment
from tf_agents.environments import tf_environment
from tf_agents.environments import tf_py_environment
from tf_agents.environments import utils
from tf_agents.specs import array_spec
from tf_agents.environments import wrappers
from tf_agents.environments import suite_gym
from tf_agents.trajectories import time_step as ts

tf.compat.v1.enable_v2_behavior()

logging.basicConfig(level=logging.DEBUG, filename='app.log', filemode='w')
# logging.debug('This will get logged to a file')

class ClusterEnv(py_environment.PyEnvironment):

    def __init__(self):
        cluster.init_cluster()
        # logging.debug('length cluster_state_min ', len(cluster.cluster_state_min))
        self._action_spec = array_spec.BoundedArraySpec(
            shape=(), dtype=np.int32, minimum=0, maximum=3, name='action')
        self._observation_spec = array_spec.BoundedArraySpec(
            shape=(cluster.features,), dtype=np.int32, minimum=cluster.cluster_state_min,
            maximum=cluster.cluster_state_max,
            name='observation')
        self._state = np.copy(cluster.cluster_state_init)
        self._episode_ended = False
        self.reward = 0
        self.vms = np.copy(cluster.VMS)
        self.jobs = np.copy(cluster.JOBS)
        self.clock = self.jobs[0].arrival_time
        self.job_idx = 0
        self.job_queue = PriorityQueue()
        self.episode_success = False

    def action_spec(self):
        return self._action_spec

    def observation_spec(self):
        return self._observation_spec

    def _reset(self):
        cluster.init_cluster()
        self._state = np.copy(cluster.cluster_state_init)
        self._episode_ended = False
        self.reward = 0
        self.vms = np.copy(cluster.VMS)
        self.jobs = np.copy(cluster.JOBS)
        self.clock = self.jobs[0].arrival_time
        self.job_idx = 0
        self.job_queue = PriorityQueue()
        self.episode_success = False
        return ts.restart(np.array(self._state, dtype=np.int32))

    def _step(self, action):

        # logging.debug("Current Cluster State: {}".format(self._state))
        if self._episode_ended:
            # The last action ended the episode. Ignore the current action and start
            # a new episode.
            return self.reset()

        if action > 3 or action < 0:
            raise ValueError('`action` should in 0 to 3.')

        elif action == 0:
            logging.debug("CLOCK: {}: Action: {}".format(self.clock, action))
            # penalty for partial placement
            if self.jobs[self.job_idx].ex_placed > 0:
                self.reward = (-200)
                self._episode_ended = True
                logging.debug("CLOCK: {}: Partial Executor Placement for a Job. Episode Ended".format(self.clock))
            # if no running jobs but jobs waiting to be scheduled -> huge Neg Reward and episode ends
            elif self.job_queue.empty():
                self.reward = (-200)
                self._episode_ended = True
                logging.debug("CLOCK: {}: No Executor Placement When No Job was Running. Episode Ended".format(self.clock))
            # finishOneJob() <- finish one running job, update cluster states-> "self._state"
            else:
                self.reward = -1
                _, y = self.job_queue.get()
                self.clock = y.finish_time
                self.finish_one_job(y)
            # TODO add check for large job which does not fit in the cluster
        else:
            logging.debug("CLOCK: {}: Action: {}".format(self.clock, action))
            # if valid placement, place 1 ex in the VM chosen, update cluster states -> "self._state";
            # check for episode end  -> update self._episode_ended
            if self.execute_placement(action):
                #print('placement successful, clock: ', self.clock)
                self.reward = 5
                self.check_episode_end()
            # if invalid placement -> Huge Neg Reward and episode ends
            else:
                self.reward = (-200)
                self._episode_ended = True
                logging.debug("CLOCK: {}: Invalid Executor Placement, Episode Ended".format(self.clock))

            # self._episode_ended = True -> when last job's last executor is placed or bad action

            # self._state = generate new state after executing the current action
        if self._episode_ended:

            if self.episode_success:
                self.reward = 100
                logging.debug("CLOCK: {}: ****** Episode ended Successfully!!!!!!!! ".format(self.clock))
            # self.calculate_reward()
            return ts.termination(np.array(self._state, dtype=np.int32), self.reward)

        else:
            return ts.transition(
                np.array(self._state, dtype=np.int32), reward=self.reward, discount=0.9)

    def finish_one_job(self, finished_job):
        finished_job.finished = True
        finished_job.running = False
        for i in range(len(finished_job.ex_placement_list)):
            vm = finished_job.ex_placement_list[i]
            vm.cpu_now += finished_job.cpu
            vm.mem_now += finished_job.mem
            self.vms[vm.id] = vm
        self._state = cluster.gen_cluster_state(self.job_idx, self.jobs,
                                                self.vms)
        logging.debug("CLOCK: {}: Finished execution of job: {}".format(self.clock, finished_job.id))
        logging.debug("CLOCK: {}: Current Cluster State: {}".format(self.clock, self._state))

    def execute_placement(self, action):
        current_job = self.jobs[self.job_idx]
        vm = self.vms[action - 1]
        if current_job.cpu > vm.cpu_now or current_job.mem > vm.mem_now:
            return False

        if not current_job.running:
            current_job.running = True
            current_job.start_time = self.clock
            current_job.finish_time = self.clock + current_job.duration
            self.job_queue.put((current_job.finish_time, current_job))

        if current_job.finish_time > vm.stop_use_clock:
            vm.used_time += (current_job.finish_time - vm.stop_use_clock)
            vm.stop_use_clock = current_job.finish_time

        current_job.ex_placed += 1
        current_job.ex_placement_list.append(vm)
        vm.cpu_now -= current_job.cpu
        vm.mem_now -= current_job.mem

        self.vms[vm.id] = vm
        self.jobs[self.job_idx] = current_job

        if current_job.ex_placed == current_job.ex:
            self.reward = 10
            logging.debug("CLOCK: {}: Finished placement of job: {}".format(self.clock, current_job.id))
            if self.job_idx+1 == len(self.jobs):
                self._episode_ended = True
                self.episode_success = True
                return True
            self.job_idx += 1
            self.clock = self.jobs[self.job_idx].arrival_time

            while True:
                if self.job_queue.empty():
                    break
                _, next_finished_job = self.job_queue.get()
                if next_finished_job.finish_time <= self.clock:
                    self.finish_one_job(next_finished_job)
                else:
                    self.job_queue.put((next_finished_job.finish_time, next_finished_job))
                    break

        self._state = cluster.gen_cluster_state(self.job_idx, self.jobs,
                                                self.vms)
        return True

    def check_episode_end(self):
        current_job = self.jobs[self.job_idx]
        if self.job_idx + 1 == len(self.jobs) and current_job.ex == current_job.ex_placed:
            self._episode_ended = True

    def calculate_reward(self):
        for i in range(len(self.vms)):
            self.reward -= (self.vms[i].price * self.vms[i].used_time)
            print('vm: ', i, ' price: ', self.vms[i].price, ' time: ', self.vms[i].used_time)
        logging.debug("Episode Reward: {}\n\n".format(self.reward))


# environment = ClusterEnv()
# environment2 = ClusterEnv()

# environment = ClusterEnv()
# utils.validate_py_environment(environment, episodes=1000)