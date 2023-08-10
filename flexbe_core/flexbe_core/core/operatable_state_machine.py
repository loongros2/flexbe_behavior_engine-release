#!/usr/bin/env python

# Copyright 2023 Philipp Schillinger, Team ViGIR, Christopher Newport University
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#    * Redistributions of source code must retain the above copyright
#      notice, this list of conditions and the following disclaimer.
#
#    * Redistributions in binary form must reproduce the above copyright
#      notice, this list of conditions and the following disclaimer in the
#      documentation and/or other materials provided with the distribution.
#
#    * Neither the name of the Philipp Schillinger, Team ViGIR, Christopher Newport University nor the names of its
#      contributors may be used to endorse or promote products derived from
#      this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.


"""OperatableStateMachine."""

import zlib
from std_msgs.msg import Empty, UInt8, Int32
from flexbe_msgs.msg import Container, ContainerStructure, BehaviorSync, CommandFeedback

from flexbe_core.core.state_machine import StateMachineError
from flexbe_core.core.operatable_state import OperatableState
from flexbe_core.core.preemptable_state_machine import PreemptableStateMachine
from flexbe_core.core.user_data import UserData
from flexbe_core.logger import Logger
from flexbe_core.state_logger import StateLogger


class OperatableStateMachine(PreemptableStateMachine):
    """
    A state machine that can be operated.

    It synchronizes its current state with the mirror and supports some control mechanisms.
    """

    autonomy_level = 3

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.id = None
        self._autonomy = {}
        self._inner_sync_request = False
        self._last_exception = None

    # construction

    @staticmethod
    def add(label, state, transitions, autonomy=None, remapping=None):
        """
        Add a state to the opened state machine.

        @type label: string
        @param label: The label of the state being added.

        @param state: An instance of a class implementing the L{State} interface.

        @param transitions: A dictionary mapping state outcomes to other state
        labels or container outcomes.

        @param autonomy: A dictionary mapping state outcomes to their required
        autonomy level

        @param remapping: A dictionary mapping local userdata keys to userdata
        keys in the container.
        """
        self = OperatableStateMachine.get_opened_container()
        PreemptableStateMachine.add(label, state, transitions, remapping)
        self._autonomy[label] = autonomy

    def _build_structure_msg(self):
        """Create a message to describe the structure of this state machine."""
        structure_msg = ContainerStructure()
        container_msg = self._add_to_structure_msg(structure_msg)
        container_msg.outcomes = self.outcomes
        structure_msg.behavior_id = self.id
        return structure_msg

    def _add_to_structure_msg(self, structure_msg):
        """
        Add this state machine and all children to the structure message.

        @type structure_msg: ContainerStructure
        @param structure_msg: The message that will finally contain the structure message.
        """
        # add self to message
        container_msg = Container()
        container_msg.path = self.path
        container_msg.children = [state.name for state in self._states]
        structure_msg.containers.append(container_msg)
        # add children to message
        for state in self._states:
            # create and add children
            if isinstance(state, OperatableStateMachine):
                state_msg = state._add_to_structure_msg(structure_msg)
            else:
                state_msg = Container(path=state.path)
                structure_msg.containers.append(state_msg)
            # complete structure info for children
            state_msg.outcomes = state.outcomes
            state_msg.transitions = [self._transitions[state.name][outcome] for outcome in state.outcomes]
            state_msg.autonomy = [self._autonomy[state.name][outcome] for outcome in state.outcomes]
        return container_msg

    def get_latest_status(self):
        """Return the latest execution information as a BehaviorSync message."""
        with self._status_lock:
            path = self._last_deep_state_path
            beh_id = self.id

        msg = BehaviorSync()
        msg.behavior_id = beh_id if beh_id is not None else 0
        msg.current_state_checksum = zlib.adler32(path.encode()) & 0x7fffffff if path is not None else 0
        return msg

    # execution
    def _execute_current_state(self):
        # catch any exception and keep state active to let operator intervene
        try:
            # --- @TODO remove self._inner_sync_request = False  # clear any prior sync request
            outcome = super()._execute_current_state()
            self._last_exception = None
        except Exception as exc:  # pylint: disable=W0703
            # Error here
            outcome = None
            self._last_exception = exc
            Logger.logerr('Failed to execute state %s:\n%s' % (self.current_state_label, str(exc)))
            import traceback  # pylint: disable=C0415
            Logger.localinfo(traceback.format_exc().replace("%", "%%"))  # Guard against exeception including format!

        return outcome

    def process_sync_request(self):
        """
        Provide explicit sync as back-up functionality.

        Should be used sparingly if there is no other choice
        since it requires additional 8 byte + header update bandwith and time to restart mirror
        """
        if self._inner_sync_request:
            if self.id is None:
                Logger.localerr("Sync requested - but why processing here - self.id is None!")

            msg = BehaviorSync()
            msg.behavior_id = self.id
            try:
                deep_state = self.get_deep_state()
                if deep_state is not None:
                    try:
                        current_label = self.current_state_label
                    except StateMachineError:  # pylint: disable=W0703
                        current_label = "None"

                    Logger.localinfo(f"Sync request processed by {self.id} {self.name} with "
                                     f"current state={current_label} (deep state = {deep_state.name})")
                    msg.current_state_checksum = zlib.adler32(deep_state.path.encode()) & 0x7fffffff

                else:
                    Logger.localwarn(f"Inner sync requested for {self.name} : - no active deep state!'")
            except Exception as exc:  # pylint: disable=W0703
                Logger.localerr(f"Inner sync requested by {self.id} - encountered error {type(exc)}:\n  {exc}")

            self._inner_sync_request = False
            self._pub.publish('flexbe/mirror/sync', msg)
            self._pub.publish('flexbe/command_feedback', CommandFeedback(command="sync", args=[]))
            Logger.localinfo("<-- Sent synchronization message for mirror.")
        else:
            Logger.error('Inner sync processed for %s - but no sync request flag?' % (self.name))

    def is_transition_allowed(self, label, outcome):
        return self._autonomy[label].get(outcome, -1) < OperatableStateMachine.autonomy_level

    def get_required_autonomy(self, outcome):
        return self._autonomy[self.current_state_label][outcome]

    def destroy(self):
        Logger.localinfo(f'Destroy state machine {self.name}: {self.id} ...')
        self._notify_stop()
        self._disable_ros_control()
        self._sub.unsubscribe_topic('flexbe/command/autonomy', inst_id=id(self))
        self._sub.unsubscribe_topic('flexbe/command/sync', inst_id=id(self))
        self._sub.unsubscribe_topic('flexbe/command/attach', inst_id=id(self))
        self._sub.unsubscribe_topic('flexbe/request_mirror_structure', inst_id=id(self))
        StateLogger.shutdown()

    def confirm(self, name, beh_id):
        """
        Confirm the state machine and triggers the creation of the structural message.

        It is mandatory to call this function at the top-level state machine
        between building it and starting its execution.

        @type name: string
        @param name: The name of this state machine to identify it.

        @type beh_id: int
        @param beh_id: The behavior id of this state machine to identify it.
        """
        self.set_name(name)
        self.id = beh_id

        Logger.localinfo(f'--> Set up pub/sub for behavior {self.name}: {self.id} ...')
        # Update mirror with currently active state (high bandwidth mode)
        self._pub.createPublisher('flexbe/mirror/sync', BehaviorSync)
        # Sends the current structure to the mirror
        self._pub.createPublisher('flexbe/mirror/structure', ContainerStructure)
        # Gives feedback about executed commands to the GUI
        self._pub.createPublisher('flexbe/command_feedback', CommandFeedback)

        self._sub.subscribe('flexbe/command/autonomy', UInt8, self._set_autonomy_level, inst_id=id(self))
        self._sub.subscribe('flexbe/command/sync', Empty, self._sync_callback, inst_id=id(self))
        self._sub.subscribe('flexbe/command/attach', UInt8, self._attach_callback, inst_id=id(self))
        self._sub.subscribe('flexbe/request_mirror_structure', Int32, self._mirror_structure_callback, inst_id=id(self))

        StateLogger.initialize(name)
        StateLogger.log('flexbe.initialize', None, behavior=name, autonomy=OperatableStateMachine.autonomy_level)
        if OperatableStateMachine.autonomy_level != 255:
            self._enable_ros_control()

        Logger.localinfo(f'--> Wait for behavior {self.name}: {self.id} publishers to activate ...')
        self.wait(seconds=0.25)  # no clean way to wait for publisher to be ready...

        Logger.localinfo(f'--> Notify behavior {self.name}: {self.id} states to start ...')
        self._notify_start()
        Logger.localinfo(f'--> behavior {self.name}: {self.id} confirmation complete!')

    # operator callbacks

    def _set_autonomy_level(self, msg):
        """Set the current autonomy level."""
        if OperatableStateMachine.autonomy_level != msg.data:
            Logger.localinfo(f'--> Request autonomy changed to {msg.data} on {self.name}')
        if msg.data < 0:
            Logger.localinfo(f'--> Negative autonomy level={msg.data} - Preempt {self.name}!')
            self._preempt_cb(msg)
        else:
            OperatableStateMachine.autonomy_level = msg.data
        self._pub.publish('flexbe/command_feedback', CommandFeedback(command="autonomy", args=[]))

    def _sync_callback(self, msg):
        Logger.localinfo(f"--> Synchronization requested ... ({self.id}) {self.name}")
        self._inner_sync_request = True  # Flag to process at the end of spin loop

    def _attach_callback(self, msg):
        Logger.localinfo("--> Enabling attach control...")
        # set autonomy level
        OperatableStateMachine.autonomy_level = msg.data
        # enable control of states
        self._enable_ros_control()
        self._inner_sync_request = True
        # send command feedback
        cfb = CommandFeedback(command="attach")
        cfb.args.append(self.name)
        self._pub.publish('flexbe/command_feedback', cfb)
        Logger.localinfo("<-- Sent attach confirm.")

    def _mirror_structure_callback(self, msg):
        Logger.localinfo(f"--> Creating behavior structure for mirror id={msg.data} ...")
        self._pub.publish('flexbe/mirror/structure', self._build_structure_msg())
        Logger.localinfo("<-- Sent behavior structure to mirror.")
        # enable control of states since a mirror is listening
        self._enable_ros_control()

    # handle state events

    def _notify_start(self):
        for state in self._states:
            if isinstance(state, OperatableState):
                state.on_start()
            if isinstance(state, OperatableStateMachine):
                state._notify_start()

    def _notify_stop(self):
        self.on_stop()
        for state in self._states:
            if isinstance(state, OperatableState):
                state.on_stop()
            if isinstance(state, OperatableStateMachine):
                state.on_stop()
                state._notify_stop()
            if state._is_controlled:
                state._disable_ros_control()

    def on_exit(self, userdata):
        if self._current_state is not None:
            udata = UserData(reference=self.userdata,
                             input_keys=self._current_state.input_keys,
                             output_keys=self._current_state.output_keys,
                             remap=self._remappings[self._current_state.name])
            self._current_state._entering = True
            self._current_state.on_exit(udata)
            self._current_state = None
