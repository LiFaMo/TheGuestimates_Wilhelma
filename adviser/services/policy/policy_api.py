###############################################################################
#
# Copyright 2020, University of Stuttgart: Institute for Natural Language Processing (IMS)
#
# This file is part of Adviser.
# Adviser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3.
#
# Adviser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Adviser.  If not, see <https://www.gnu.org/licenses/>.
#
###############################################################################

from typing import List, Dict

from utils.domain.lookupdomain import LookupDomain
from services.service import PublishSubscribe, Service
from utils import SysAct, SysActionType
from utils.logger import DiasysLogger
from utils.beliefstate import BeliefState
from utils.useract import UserActionType
from collections import defaultdict


class HandcraftedPolicy(Service):
    """Handcrafted policy for API domains

    Differs from the default HandcraftedPolicy class by taking into account mandatory slots,
    i.e. slots which have to be informed about before an API can even be called.
    The class is currently a copy of an older version of the HandcraftedPolicy class with the
    required changes for API usage.
    The classes will probably be merged in the future.
    """

    def __init__(self, domain: LookupDomain, logger: DiasysLogger = DiasysLogger()):
        """
        Initializes the policy

        Arguments:
            domain {domain.lookupdomain.LookupDomain} -- Domain

        """
        self.first_turn = True
        Service.__init__(self, domain=domain)
        self.last_action = None
        self.current_suggestions = []  # list of current suggestions
        self.s_index = 0  # the index in current suggestions for the current system reccomendation
        self.domain_key = domain.get_primary_key()
        self.logger = logger

    @PublishSubscribe(sub_topics=["beliefstate"], pub_topics=["sys_act", "sys_state"])
    def choose_sys_act(self, beliefstate: BeliefState = None, sys_act: SysAct = None)\
            -> dict(sys_act=SysAct):

        """
            Responsible for walking the policy through a single turn. Uses the current user
            action and system belief state to determine what the next system action should be.

            To implement an alternate policy, this method may need to be overwritten

            Args:
                belief_state (BeliefState): a BeliefState object representing current system
                                           knowledge
                user_acts (list): a list of UserAct objects mapped from the user's last utterance
                sys_act (SysAct): this should be None

            Returns:
                (dict): a dictionary with the key "sys_act" and the value that of the systems next
                        action

        """
        # variables for general (non-domain specific) actions
        # self.turn = dialog_graph.num_turns
        self.prev_sys_act = sys_act
        self._remove_gen_actions(beliefstate)
        sys_state = {}

        # do nothing on the first turn --LV
        if self.first_turn and not beliefstate['user_acts']:
            self.first_turn = False
            sys_act = SysAct()
            sys_act.type = SysActionType.Welcome
            return {'sys_act': sys_act}
        elif UserActionType.Bad in beliefstate["user_acts"]:
            sys_act = SysAct()
            sys_act.type = SysActionType.Bad
        # if the action is 'bye' tell system to end dialog
        elif UserActionType.Bye in beliefstate["user_acts"]:
            sys_act = SysAct()
            sys_act.type = SysActionType.Bye
        # if user only says thanks, ask if they want anything else
        elif UserActionType.Thanks in beliefstate["user_acts"]:
            sys_act = SysAct()
            sys_act.type = SysActionType.RequestMore
        # If user only says hello, request a random slot to move dialog along
        elif UserActionType.Hello in beliefstate["user_acts"] or UserActionType.SelectDomain in beliefstate["user_acts"]:
            sys_act = SysAct()
            sys_act.type = SysActionType.Request
            slot = self._get_open_slot(beliefstate)
            sys_act.add_value(slot)
            # prepare sys_state info
            sys_state['lastRequestSlot'] = slot

            # If we switch to the domain, start a new dialog
            if UserActionType.SelectDomain in beliefstate["user_acts"]:
                self.dialog_start()
            self.first_turn = False

        # handle domain specific actions
        else:
            sys_act, sys_state = self._next_action(beliefstate)

        self.logger.dialog_turn("System Action: " + str(sys_act))
        if 'last_act' not in sys_state:
            sys_state['last_act'] = sys_act
        return {'sys_act': sys_act, 'sys_state': sys_state}

    def dialog_start(self):
        self.first_turn = True
        self.last_action = None
        self.current_suggestions = []  # list of current suggestions
        self.s_index = 0  # the index in current suggestions for the current system reccomendation

    def _remove_gen_actions(self, beliefstate: BeliefState):
        """
            Helper function to read through user action list and if necessary
            delete filler actions (eg. Hello, thanks) when there are other non-filler
            (eg. Inform, Request) actions from the user. Stores list of relevant actions
            as a class variable

            Args:
                user_acts (list): a list of UserAct objects

        """
        act_types_lst = beliefstate["user_acts"]
        # These are filler actions, so if there are other non-filler acions, remove them from
        # the list of action types
        while len(act_types_lst) > 1:
            if UserActionType.Thanks in act_types_lst:
                act_types_lst.remove(UserActionType.Thanks)
            elif UserActionType.Bad in act_types_lst:
                act_types_lst.remove(UserActionType.Bad)
            elif UserActionType.Hello in act_types_lst:
                act_types_lst.remove(UserActionType.Hello)
            else:
                break

    def _query_db(self, beliefstate: BeliefState):
        """Based on the constraints specified, uses self.domain to generate the appropriate type
           of query for the database

        Returns:
            iterable: representing the results of the database lookup

        --LV
        """
        # determine if an entity has already been suggested or was mentioned by the user
        name = self._get_name(beliefstate)
        # if yes and the user is asking for info about a specific entity, generate a query to get
        # that info for the slots they have specified
        if name and beliefstate['requests']:
            requested_slots = beliefstate['requests']
            return self.domain.find_info_about_entity(name, requested_slots)
        # otherwise, issue a query to find all entities which satisfy the constraints the user
        # has given so far
        else:
            constraints, _ = self._get_constraints(beliefstate)
            return self.domain.find_entities(constraints)

    def _get_name(self, beliefstate: BeliefState):
        """Finds if an entity has been suggested by the system (in the form of an offer candidate)
           or by the user (in the form of an InformByName act). If so returns the identifier for
           it, otherwise returns None

        Args:
            belief_state (dict): dictionary tracking the current system beliefs

        Return:
            (str): Returns a string representing the current entity name

        -LV
        """
        name = None
        prim_key = self.domain.get_primary_key()
        if prim_key in beliefstate['informs']:
            possible_names = beliefstate['informs'][prim_key]
            name = sorted(possible_names.items(), key=lambda kv: kv[1], reverse=True)[0][0]
        # if the user is trying to query by name
        else:
            if self.s_index < len(self.current_suggestions):
                current_suggestion = self.current_suggestions[self.s_index]
                if current_suggestion:
                    name = current_suggestion[self.domain_key]
        return name

    def _get_constraints(self, beliefstate: BeliefState):
        """Reads the belief state and extracts any user specified constraints and any constraints
           the user indicated they don't care about, so the system knows not to ask about them

        Args:
            belief_state (dict): dictionary tracking the current system beliefs

        Return:
            (tuple): dict of user requested slot names and their values and list of slots the user
                     doesn't care about

        --LV
        """
        slots = {}
        # parts of the belief state which don't contain constraints
        dontcare = [slot for slot in beliefstate['informs'] if "dontcare" in beliefstate["informs"][slot]]
        informs = beliefstate["informs"]
        slots = {}
        for slot in informs:
            if slot not in dontcare:
                for value in informs[slot]:
                    slots[slot] = value
        return slots, dontcare

    def _mandatory_requests_fulfilled(self, belief_state: BeliefState):
        """whether or not all mandatory slots have a value

        Arguments:
            beliefstate (BeliefState): dictionary tracking the current system beliefs
        """
        filled_slots, _ = self._get_constraints(belief_state)
        mandatory_slots = self.domain.get_mandatory_slots()
        for slot in mandatory_slots:
            if slot not in filled_slots:
                return False
        return True

    def _get_open_mandatory_slot(self, belief_state: BeliefState):
        """

        Args:
            belief_state (dict): dictionary tracking the current system beliefs

        Returns:
            (str): a string representing a category the system might want more info on. If all
            system requestables have been filled, return none

        """
        filled_slots, _ = self._get_constraints(belief_state)
        mandatory_slots = self.domain.get_mandatory_slots()
        for slot in mandatory_slots:
            if slot not in filled_slots:
                return slot
        return None

    def _get_open_slot(self, belief_state: BeliefState):
        """For a hello statement we need to be able to figure out what slots the user has not yet
           specified constraint for, this method returns one of those at random

        Args:
            belief_state (dict): dictionary tracking the current system beliefs

        Returns:
            (str): a string representing a category the system might want more info on. If all
            system requestables have been filled, return none

        """
        filled_slots, _ = self._get_constraints(belief_state)
        requestable_slots = self.domain.get_system_requestable_slots()
        for slot in requestable_slots:
            if slot not in filled_slots:
                return slot
        return None

    def _next_action(self, beliefstate: BeliefState):
        """Determines the next system action based on the current belief state and
           previous action.

           When implementing a new type of policy, this method MUST be rewritten

        Args:
            belief_state (HandCraftedBeliefState): system values on liklihood
            of each possible state

        Return:
            (SysAct): the next system action

        --LV
        """
        sys_state = {}
        # Assuming this happens only because domain is not actually active --LV
        """if UserActionType.Bad in beliefstate['user_acts'] or beliefstate['requests'] \
                and not self._get_name(beliefstate):
            sys_act = SysAct()
            sys_act.type = SysActionType.Bad
            return sys_act, {'last_action': sys_act}"""

        if not self._mandatory_requests_fulfilled(beliefstate):
            sys_act = SysAct()
            sys_act.type = SysActionType.Request
            sys_act.slot_values = {self._get_open_mandatory_slot(beliefstate): None}
            return sys_act, {'last_action': sys_act}

        elif UserActionType.RequestAlternatives in beliefstate['user_acts'] \
                and not self._get_constraints(beliefstate)[0]:
            sys_act = SysAct()
            sys_act.type = SysActionType.Bad
            return sys_act, {'last_action': sys_act}

        elif self.domain.get_primary_key() in beliefstate['informs'] \
                and not beliefstate['requests']:
            sys_act = SysAct()
            sys_act.type = SysActionType.InformByName
            sys_act.add_value(self.domain.get_primary_key(), self._get_name(beliefstate))
            return sys_act, {'last_action': sys_act}

        # Otherwise we need to query the db to determine next action
        results = self._query_db(beliefstate)
        sys_act = self._raw_action(results, beliefstate)

        # requests are fairly easy, if it's a request, return it directly
        if sys_act.type == SysActionType.Request:
            if len(list(sys_act.slot_values.keys())) > 0:
                # update the belief state to reflec the slot we just asked about
                sys_state['lastRequestSlot'] = list(sys_act.slot_values.keys())[0]
                # belief_state['system']['lastRequestSlot'] = list(sys_act.slot_values.keys())[0]

        # otherwise we need to convert a raw inform into a one with proper slots and values
        elif sys_act.type == SysActionType.InformByName:
            self._convert_inform(results, sys_act, beliefstate)
            # update belief state to reflect the offer we just made
            values = sys_act.get_values(self.domain.get_primary_key())
            if values:
                # belief_state['system']['lastInformedPrimKeyVal'] = values[0]
                sys_state['lastInformedPrimKeyVal'] = values[0]
            else:
                sys_act.add_value(self.domain.get_primary_key(), 'none')

        sys_state['last_act'] = sys_act
        return (sys_act, sys_state)

    def _raw_action(self, q_res: iter, beliefstate: BeliefState) -> SysAct:
        """Based on the output of the db query and the method, choose
           whether next action should be request or inform

        Args:
            q_res (list): rows (list of dicts) returned by the issued sqlite3
            query
            method (str): the type of user action
                     ('byprimarykey', 'byconstraints', 'byalternatives')

        Returns:
            (SysAct): SysAct object of appropriate type

        --LV
        """
        sys_act = SysAct()
        # if there is more than one result
        if len(q_res) > 1:
            constraints, dontcare = self._get_constraints(beliefstate)
            # Gather all the results for each column
            temp = {key: [] for key in q_res[0].keys()}
            # If any column has multiple values, ask for clarification
            for result in q_res:
                for key in result.keys():
                    if key != self.domain_key:
                        temp[key].append(result[key])
            next_req = self._gen_next_request(temp, beliefstate)
            if next_req:
                sys_act.type = SysActionType.Request
                sys_act.add_value(next_req)
                return sys_act

        # Otherwise action type will be inform, so return an empty inform (to be filled in later)
        sys_act.type = SysActionType.InformByName
        return sys_act

    def _gen_next_request(self, temp: Dict[str, List[str]], belief_state: BeliefState):
        """
            Calculates which slot to request next based asking for non-binary slotes first and then
            based on which binary slots provide the biggest reduction in the size of db results

            NOTE: If the dataset is large, this is probably not a great idea to calculate each turn
                  it's relatively simple, but could add up over time

            Args:
                temp (Dict[str, List[str]]: a dictionary with the keys and values for each result
                                            in the result set

            Returns: (str) representing the slot to ask for next (or empty if none)
        """
        req_slots = self.domain.get_system_requestable_slots()
        # don't other to cacluate statistics for things which have been specified
        constraints, dontcare = self._get_constraints(belief_state)
        # split out binary slots so we can ask about them second
        req_slots = [s for s in req_slots if s not in dontcare and s not in constraints]
        bin_slots = [slot for slot in req_slots if len(self.domain.get_possible_values(slot)) == 2]
        non_bin_slots = [slot for slot in req_slots if slot not in bin_slots]

        # check if there are any differences in values for non-binary slots,
        # if a slot has multiple values, ask about that slot
        for slot in non_bin_slots:
            if len(set(temp[slot])) > 1:
                return slot
        # Otherwise look to see if there are differnces in binary slots
        return self._highest_info_gain(bin_slots, temp)

    def _highest_info_gain(self, bin_slots: List[str], temp: Dict[str, List[str]]):
        """ Since we don't have lables, we can't properlly calculate entropy, so instead we'll go
            for trying to ask after a feature that splits the results in half as evenly as possible
            (that way we gain most info regardless of which way the user chooses)

            Args:
                bin_slots: a list of strings representing system requestable binary slots which
                           have not yet been specified
                temp (Dict[str, List[str]]: a dictionary with the keys and values for each result
                                            in the result set

            Returns: (str) representing the slot to ask for next (or empty if none)
        """
        diffs = {}
        for slot in bin_slots:
            val1, val2 = self.domain.get_possible_values(slot)
            values_dic = defaultdict(int)
            for val in temp[slot]:
                values_dic[val] += 1
            if val1 in values_dic and val2 in values_dic:
                diffs[slot] = abs(values_dic[val1] - values_dic[val2])
            # If all slots have the same value, we don't need to request anything, return none
        if not diffs:
            return ""
        sorted_diffs = sorted(diffs.items(), key=lambda kv: kv[1])
        return sorted_diffs[0][0]

    def _convert_inform(self, q_results: iter,
                        sys_act: SysAct, beliefstate: BeliefState):
        """Fills in the slots and values for a raw inform so it can be returned as the
           next system action.

        Args:
            method (str): the type of user action
                     ('byprimarykey', 'byconstraints', 'byalternatives')
            q_results (list): Results of SQL database query
            sys_act (SysAct): the act to be modified
            belief_state(dict): contains info on what columns were queried

        --LV
        """
        """beliefstate["requests"] or """
        if self.domain.get_primary_key() in beliefstate['informs']:
            self._convert_inform_by_primkey(q_results, sys_act, beliefstate)

        elif UserActionType.RequestAlternatives in beliefstate['user_acts']:
            self._convert_inform_by_alternatives(sys_act, q_results, beliefstate)

        else:
            self._convert_inform_by_constraints(q_results, sys_act, beliefstate)

    def _convert_inform_by_primkey(self, q_results: iter,
                                   sys_act: SysAct, belief_state: BeliefState):
        """
            Helper function that adds the values for slots to a SysAct object when the system
            is answering a request for information about an entity from the user

            Args:
                q_results (iterable): list of query results from the database
                sys_act (SysAct): current raw sys_act to be filled in
                belief_state (BeliefState)

        """
        sys_act.type = SysActionType.InformByName
        if q_results:
            result = q_results[0]  # currently return just the first result
            keys = list(result.keys())  # should represent all user specified constraints

            # add slots + values (where available) to the sys_act
            for k in keys:
                res = result[k] if result[k] else 'not available'
                sys_act.add_value(k, res)
            # Name might not be a constraint in request queries, so add it
            if self.domain_key not in keys:
                name = self._get_name(belief_state)
                sys_act.add_value(self.domain_key, name)
            # Add default Inform slots
            for slot in self.domain.get_default_inform_slots():
                if slot not in sys_act.slot_values:
                    sys_act.add_value(slot, result[slot])
        else:
            sys_act.add_value(self.domain_key, 'none')

    def _convert_inform_by_alternatives(
            self, sys_act: SysAct, q_res: iter, belief_state: BeliefState):
        """
            Helper Function, scrolls through the list of alternative entities which match the
            user's specified constraints and uses the next item in the list to fill in the raw
            inform act.

            When the end of the list is reached, currently continues to give last item in the list
            as a suggestion

            Args:
                sys_act (SysAct): the raw inform to be filled in
                belief_state (BeliefState): current system belief state ()

        """
        if q_res and not self.current_suggestions:
            self.current_suggestions = []
            self.s_index = -1
            for result in q_res:
                self.current_suggestions.append(result)

        self.s_index += 1
        # here we should scroll through possible offers presenting one each turn the user asks
        # for alternatives
        if self.s_index <= len(self.current_suggestions) - 1:
            # the first time we inform, we should inform by name, so we use the right template
            if self.s_index == 0:
                sys_act.type = SysActionType.InformByName
            else:
                sys_act.type = SysActionType.InformByAlternatives
            result = self.current_suggestions[self.s_index]
            # Inform by alternatives according to our current templates is
            # just a normal inform apparently --LV
            sys_act.add_value(self.domain_key, result[self.domain_key])
        else:
            sys_act.type = SysActionType.InformByAlternatives
            # default to last suggestion in the list
            self.s_index = len(self.current_suggestions) - 1
            sys_act.add_value(self.domain.get_primary_key(), 'none')

        # in addition to the name, add the constraints the user has specified, so they know the
        # offer is relevant to them
        constraints, dontcare = self._get_constraints(belief_state)
        for c in constraints:
            sys_act.add_value(c, constraints[c])

    def _convert_inform_by_constraints(self, q_results: iter,
                                       sys_act: SysAct, belief_state: BeliefState):
        """
            Helper function for filling in slots and values of a raw inform act when the system is
            ready to make the user an offer

            Args:
                q_results (iter): the results from the databse query
                sys_act (SysAct): the raw infor act to be filled in
                belief_state (BeliefState): the current system beliefs

        """
        if list(q_results):
            self.current_suggestions = []
            self.s_index = 0
            for result in q_results:
                self.current_suggestions.append(result)
            result = self.current_suggestions[0]
            sys_act.add_value(self.domain_key, result[self.domain_key])
            # Add default Inform slots
            for slot in self.domain.get_default_inform_slots():
                if slot not in sys_act.slot_values:
                    sys_act.add_value(slot, result[slot])
        else:
            sys_act.add_value(self.domain_key, 'none')

        sys_act.type = SysActionType.InformByName
        constraints, dontcare = self._get_constraints(belief_state)
        for c in constraints:
            # Using constraints here rather than results to deal with empty
            # results sets (eg. user requests something impossible) --LV
            sys_act.add_value(c, constraints[c])

        if self.current_suggestions:
            for slot in belief_state['requests']:
                if slot not in sys_act.slot_values:
                    sys_act.add_value(slot, self.current_suggestions[0][slot])
