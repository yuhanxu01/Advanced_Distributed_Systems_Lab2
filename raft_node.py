# -*- coding:utf-8 -*-
"""
CISC 6935 Distributed Systems - Lab 2
Raft Consensus Algorithm Implementation
Core Raft node with leader election, log replication, and state management
"""

import time
import random
import threading
import json
import os
from enum import Enum
from typing import List, Dict, Optional
from multiprocessing.connection import Client
import pickle


class NodeState(Enum):
    """Raft node states"""
    FOLLOWER = "follower"
    CANDIDATE = "candidate"
    LEADER = "leader"


class LogEntry:
    """Raft log entry with term, index, and value"""
    def __init__(self, term: int, index: int, value: str):
        self.term = term
        self.index = index
        self.value = value

    def to_dict(self):
        return {"term": self.term, "index": self.index, "value": self.value}

    @staticmethod
    def from_dict(data):
        return LogEntry(data["term"], data["index"], data["value"])

    def __repr__(self):
        return f"LogEntry(term={self.term}, idx={self.index}, val={self.value})"


class RPCProxy:
    """Proxy for making RPC calls to peer nodes"""
    def __init__(self, connection):
        self._connection = connection
        self._lock = threading.Lock()

    def __getattr__(self, name):
        def do_rpc(*args, **kwargs):
            # Simulate network delay (Requirement 3e)
            # Random delay between 0-2ms for stable "everything goes well" scenario
            # (Use 0-50ms for testing slow network conditions)
            network_delay = random.uniform(0.0, 0.002)
            time.sleep(network_delay)

            with self._lock:
                try:
                    self._connection.send(pickle.dumps((name, args, kwargs)))

                    # Add timeout to prevent blocking indefinitely
                    if self._connection.poll(timeout=2.0):  # 2 second timeout
                        result = pickle.loads(self._connection.recv())
                        if isinstance(result, Exception):
                            raise result
                        return result
                    else:
                        raise ConnectionError(f"RPC timeout: no response within 2s")
                except (EOFError, ConnectionResetError, BrokenPipeError) as e:
                    raise ConnectionError(f"Connection lost: {str(e)}")
        return do_rpc


class RaftNode:
    """
    Raft consensus node implementation
    Manages leader election, log replication, and state machine
    """

    def __init__(self, node_id: int, peers: List[tuple], port: int, wait_for_cluster: bool = True):
        # Node identification
        self.node_id = node_id
        self.peers = peers  # List of (peer_id, host, port)
        self.port = port
        self.wait_for_cluster = wait_for_cluster  # Wait for all nodes before starting election

        # Persistent state (on all servers)
        self.current_term = 0
        self.voted_for = None
        self.log = []  # List[LogEntry]

        # Volatile state (on all servers)
        self.commit_index = -1  # Index of highest log entry known to be committed
        self.last_applied = -1  # Index of highest log entry applied to state machine

        # Volatile state (on leaders)
        self.next_index = {}  # {peer_id: next log index to send}
        self.match_index = {}  # {peer_id: highest log index replicated}

        # Node state
        self.state = NodeState.FOLLOWER
        self.leader_id = None
        self.last_heartbeat = time.time()

        # Election timeout (300-600ms for stable elections with minimal network delay)
        # (Use 150-300ms only with perfect network, 0ms delay)
        self.election_timeout = random.uniform(5, 7)
        self.heartbeat_interval = 0.5  # 50ms heartbeat

        # Vote tracking
        self.votes_received = set()

        # Test mode flag (for Scenario C)
        self.pause_replication = False  # Prevents heartbeats from replicating uncommitted entries

        # Log file path (CISC6935)
        self.log_file = f"CISC6935_node{node_id}.log"
        self.state_file = f"raft_state_node{node_id}.json"

        # Thread synchronization
        self.lock = threading.Lock()

        # RPC connections to peers
        self.peer_connections = {}  # {peer_id: RPCProxy}

        # Timers
        self.election_timer_thread = None
        self.heartbeat_timer_thread = None
        self.running = False
        self.cluster_ready = False  # Flag to indicate all peers are reachable

        print(f"[Node {self.node_id}] Initialized at port {self.port}")

    # ==================== Persistence ====================

    def save_state(self):
        """Persist current term, voted_for, and log to disk"""
        state = {
            "current_term": self.current_term,
            "voted_for": self.voted_for,
            "log": [e.to_dict() for e in self.log]
        }
        with open(self.state_file, 'w') as f:
            json.dump(state, f, indent=2)

    def load_state(self):
        """Load persistent state from disk"""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                    self.current_term = state["current_term"]
                    self.voted_for = state["voted_for"]
                    self.log = [LogEntry.from_dict(e) for e in state["log"]]
                print(f"[Node {self.node_id}] Loaded state: term={self.current_term}, log_len={len(self.log)}")
            except Exception as e:
                print(f"[Node {self.node_id}] Error loading state: {e}")

        # Recover last_applied by counting entries in state machine log file
        # This prevents re-applying already committed entries after restart
        # NOTE: We do NOT recover commit_index - it's volatile state that will be
        # updated by the leader via AppendEntries RPC
        if os.path.exists(self.log_file):
            try:
                with open(self.log_file, 'r') as f:
                    lines = f.readlines()
                    applied_count = len([line for line in lines if line.strip()])
                    if applied_count > 0:
                        self.last_applied = applied_count - 1  # 0-indexed
                        print(f"[Node {self.node_id}] Recovered last_applied={self.last_applied} from state machine log")
                        print(f"[Node {self.node_id}] commit_index will be updated by leader via AppendEntries")
            except Exception as e:
                print(f"[Node {self.node_id}] Error recovering last_applied: {e}")

    def apply_to_state_machine(self, entry: LogEntry):
        """Apply committed log entry to state machine (write to CISC6935 file)"""
        with open(self.log_file, 'a') as f:
            f.write(f"{entry.value}\n")
        print(f"[Node {self.node_id}] Applied to state machine: idx={entry.index}, val={entry.value}")

    def truncate_state_machine_log(self, new_last_applied: int):
        """
        Truncate state machine log to match truncated Raft log
        This is called when Raft log is truncated due to conflicts
        """
        if new_last_applied < 0:
            # Clear entire state machine log
            open(self.log_file, 'w').close()
            self.last_applied = -1
            print(f"[Node {self.node_id}] Cleared state machine log (truncated to -1)")
        else:
            # Read existing entries and keep only first (new_last_applied + 1) entries
            try:
                if os.path.exists(self.log_file):
                    with open(self.log_file, 'r') as f:
                        lines = f.readlines()
                    # Keep only the first (new_last_applied + 1) lines
                    with open(self.log_file, 'w') as f:
                        f.writelines(lines[:new_last_applied + 1])
                    self.last_applied = new_last_applied
                    print(f"[Node {self.node_id}] Truncated state machine log to index {new_last_applied}")
            except Exception as e:
                print(f"[Node {self.node_id}] Error truncating state machine log: {e}")

    # ==================== RPC Handlers ====================

    def request_vote(self, term: int, candidate_id: int,
                     last_log_index: int, last_log_term: int) -> dict:
        """
        RequestVote RPC handler
        Invoked by candidates to gather votes
        """
        with self.lock:
            print(f"[Node {self.node_id}] RequestVote from candidate {candidate_id}, term={term}")

            # Rule 1: Reply false if term < currentTerm
            if term < self.current_term:
                print(f"[Node {self.node_id}] Rejected vote: term too old")
                return {"term": self.current_term, "vote_granted": False}

            # Rule 2: If term > currentTerm, update and convert to follower
            if term > self.current_term:
                print(f"[Node {self.node_id}] Updating term {self.current_term} -> {term}")
                self.current_term = term
                self.voted_for = None
                self.state = NodeState.FOLLOWER
                self.leader_id = None
                self.pause_replication = False  # Clear pause flag when stepping down
                self.save_state()

            # Rule 3: Check if candidate's log is at least as up-to-date
            my_last_log_index = len(self.log) - 1
            my_last_log_term = self.log[-1].term if self.log else 0

            log_is_ok = (last_log_term > my_last_log_term) or \
                        (last_log_term == my_last_log_term and last_log_index >= my_last_log_index)

            # Rule 4: Grant vote if haven't voted (or voted for this candidate) and log is ok
            if (self.voted_for is None or self.voted_for == candidate_id) and log_is_ok:
                self.voted_for = candidate_id
                self.last_heartbeat = time.time()  # Reset election timeout
                self.save_state()
                print(f"[Node {self.node_id}] Granted vote to candidate {candidate_id}")
                return {"term": self.current_term, "vote_granted": True}

            print(f"[Node {self.node_id}] Denied vote to candidate {candidate_id}")
            return {"term": self.current_term, "vote_granted": False}

    def append_entries(self, term: int, leader_id: int, prev_log_index: int,
                       prev_log_term: int, entries: List[dict],
                       leader_commit: int) -> dict:
        """
        AppendEntries RPC handler
        Invoked by leader for log replication and heartbeat
        """
        with self.lock:
            is_heartbeat = len(entries) == 0
            msg_type = "Heartbeat" if is_heartbeat else "AppendEntries"

            if not is_heartbeat:
                print(f"[Node {self.node_id}] {msg_type} from leader {leader_id}, "
                      f"term={term}, entries={len(entries)}")

            # Rule 1: Reply false if term < currentTerm
            if term < self.current_term:
                return {"term": self.current_term, "success": False}

            # Rule 2: Update term and convert to follower if needed
            if term >= self.current_term:
                self.current_term = term
                self.state = NodeState.FOLLOWER
                self.leader_id = leader_id
                self.voted_for = None
                self.last_heartbeat = time.time()
                self.pause_replication = False  # Clear pause flag when becoming follower
                self.save_state()

            # Rule 3: Log consistency check
            # Check if log contains entry at prev_log_index with matching term
            if prev_log_index >= 0:
                if prev_log_index >= len(self.log):
                    # Log doesn't have entry at prev_log_index
                    print(f"[Node {self.node_id}] Log too short: need idx {prev_log_index}, have {len(self.log)}")
                    return {"term": self.current_term, "success": False}

                if self.log[prev_log_index].term != prev_log_term:
                    # Found conflicting entry, delete it and all following
                    # SAFETY CHECK: Never delete committed entries
                    if prev_log_index <= self.commit_index:
                        print(f"[Node {self.node_id}] ERROR: Attempt to delete committed entry at idx {prev_log_index}")
                        print(f"[Node {self.node_id}] commit_index={self.commit_index}, refusing AppendEntries")
                        return {"term": self.current_term, "success": False}

                    print(f"[Node {self.node_id}] Conflict at idx {prev_log_index}: "
                          f"expected term {prev_log_term}, got {self.log[prev_log_index].term}")

                    # Truncate Raft log
                    self.log = self.log[:prev_log_index]

                    # If truncation affects applied entries, truncate state machine log too
                    if prev_log_index <= self.last_applied:
                        new_last_applied = prev_log_index - 1
                        self.truncate_state_machine_log(new_last_applied)

                    self.save_state()
                    return {"term": self.current_term, "success": False}

            # Rule 4: Append new entries (if any)
            if entries:
                # Delete any conflicting entries and append new ones
                insert_index = prev_log_index + 1

                for i, entry_dict in enumerate(entries):
                    entry = LogEntry.from_dict(entry_dict)
                    current_index = insert_index + i

                    if current_index < len(self.log):
                        # Check for conflict
                        if self.log[current_index].term != entry.term:
                            # SAFETY CHECK: Never delete committed entries
                            if current_index <= self.commit_index:
                                print(f"[Node {self.node_id}] ERROR: Attempt to delete committed entry at idx {current_index}")
                                print(f"[Node {self.node_id}] This should never happen in correct Raft!")
                                return {"term": self.current_term, "success": False}

                            # Delete this entry and all following from Raft log
                            self.log = self.log[:current_index]

                            # If truncation affects applied entries, truncate state machine log too
                            if current_index <= self.last_applied:
                                new_last_applied = current_index - 1
                                self.truncate_state_machine_log(new_last_applied)

                            self.log.append(entry)
                    else:
                        # Append new entry
                        self.log.append(entry)

                self.save_state()
                print(f"[Node {self.node_id}] Appended {len(entries)} entries, log_len={len(self.log)}")
                print(f"[Node {self.node_id}] DEBUG: Log entries after append: {[(e.index, e.value) for e in self.log]}")

            # Rule 5: Update commit index
            if leader_commit > self.commit_index:
                old_commit = self.commit_index
                self.commit_index = min(leader_commit, len(self.log) - 1)

                if self.commit_index > old_commit:
                    print(f"[Node {self.node_id}] Updated commit_index: {old_commit} -> {self.commit_index}")
                    self.apply_committed_entries()

            return {"term": self.current_term, "success": True}

    # ==================== Leader Election ====================

    def start_election(self):
        """Start a new election by becoming candidate and requesting votes"""
        with self.lock:
            self.state = NodeState.CANDIDATE
            self.current_term += 1
            self.voted_for = self.node_id
            self.votes_received = {self.node_id}
            self.election_timeout = random.uniform(5, 7)

            current_term = self.current_term
            last_log_index = len(self.log) - 1
            last_log_term = self.log[-1].term if self.log else 0

            self.save_state()

            print(f"[Node {self.node_id}] Starting election for term {current_term}")

        # Send RequestVote RPCs to all peers
        for peer_id, host, port in self.peers:
            threading.Thread(
                target=self.send_request_vote,
                args=(peer_id, current_term, last_log_index, last_log_term),
                daemon=True
            ).start()

    def send_request_vote(self, peer_id: int, term: int,
                          last_log_index: int, last_log_term: int):
        """Send RequestVote RPC to a peer"""
        try:
            print(f"[Node {self.node_id}] Sending RequestVote to Node {peer_id} for term {term}")

            # Get or create connection to peer
            proxy = self.get_peer_connection(peer_id)
            if proxy is None:
                print(f"[Node {self.node_id}] Failed to connect to Node {peer_id}")
                return

            # Make RPC call
            response = proxy.request_vote(term, self.node_id, last_log_index, last_log_term)

            print(f"[Node {self.node_id}] Got vote response from Node {peer_id}: vote_granted={response['vote_granted']}, term={response['term']}")

            with self.lock:
                # Check if still candidate and term matches
                if self.state != NodeState.CANDIDATE or term != self.current_term:
                    return

                # If peer has higher term, step down
                if response["term"] > self.current_term:
                    print(f"[Node {self.node_id}] Discovered higher term {response['term']}, stepping down")
                    self.current_term = response["term"]
                    self.state = NodeState.FOLLOWER
                    self.voted_for = None
                    self.pause_replication = False  # Clear pause flag when stepping down
                    self.save_state()
                    return

                # If vote granted, record it
                if response["vote_granted"]:
                    self.votes_received.add(peer_id)
                    print(f"[Node {self.node_id}] Received vote from Node {peer_id}, "
                          f"total votes: {len(self.votes_received)}/{len(self.peers) + 1}")

                    # Check if won election (majority of votes)
                    if len(self.votes_received) > (len(self.peers) + 1) // 2:
                        self.become_leader()

        except Exception as e:
            print(f"[Node {self.node_id}] Error sending RequestVote to Node {peer_id}: {e}")
            # Remove failed connection from cache to retry later
            with self.lock:
                if peer_id in self.peer_connections:
                    del self.peer_connections[peer_id]

    def become_leader(self):
        """Transition to leader state"""
        if self.state != NodeState.CANDIDATE:
            return

        self.state = NodeState.LEADER
        self.leader_id = self.node_id

        # Initialize next_index and match_index for all peers
        for peer_id, _, _ in self.peers:
            self.next_index[peer_id] = len(self.log)
            self.match_index[peer_id] = -1

        print(f"[Node {self.node_id}] *** BECAME LEADER for term {self.current_term} ***")

        # Send immediate heartbeat to establish authority
        self.send_heartbeats()

    # ==================== Log Replication ====================

    def send_heartbeats(self):
        """Leader sends heartbeat (empty AppendEntries) to all followers"""
        if self.state != NodeState.LEADER:
            return

        # Skip heartbeats if replication is paused (for testing uncommitted entries)
        if self.pause_replication:
            return

        for peer_id, _, _ in self.peers:
            threading.Thread(
                target=self.send_append_entries,
                args=(peer_id,),
                daemon=True
            ).start()

    def send_append_entries(self, peer_id: int):
        """Send AppendEntries RPC to a peer (for heartbeat or log replication)"""
        with self.lock:
            if self.state != NodeState.LEADER:
                return

            # Get next index to send to this peer
            next_idx = self.next_index[peer_id]
            prev_log_index = next_idx - 1
            prev_log_term = self.log[prev_log_index].term if prev_log_index >= 0 else 0

            # Prepare entries to send
            entries = []
            if next_idx < len(self.log):
                entries = [e.to_dict() for e in self.log[next_idx:]]

            args = {
                "term": self.current_term,
                "leader_id": self.node_id,
                "prev_log_index": prev_log_index,
                "prev_log_term": prev_log_term,
                "entries": entries,
                "leader_commit": self.commit_index
            }

            current_term = self.current_term

        try:
            # Get or create connection to peer
            proxy = self.get_peer_connection(peer_id)
            if proxy is None:
                return

            # Make RPC call
            response = proxy.append_entries(**args)

            with self.lock:
                # Check if still leader and term matches
                if self.state != NodeState.LEADER or current_term != self.current_term:
                    return

                # If peer has higher term, step down
                if response["term"] > self.current_term:
                    print(f"[Node {self.node_id}] Discovered higher term {response['term']}, stepping down")
                    self.current_term = response["term"]
                    self.state = NodeState.FOLLOWER
                    self.voted_for = None
                    self.leader_id = None
                    self.pause_replication = False  # Clear pause flag when stepping down
                    self.save_state()
                    return

                # Handle response
                if response["success"]:
                    # Update next_index and match_index
                    self.match_index[peer_id] = prev_log_index + len(entries)
                    self.next_index[peer_id] = self.match_index[peer_id] + 1

                    # Try to commit entries
                    self.update_commit_index()
                else:
                    # Decrement next_index and retry
                    self.next_index[peer_id] = max(0, self.next_index[peer_id] - 1)

        except Exception as e:
            # Connection error, remove from cache and will retry on next heartbeat
            with self.lock:
                if peer_id in self.peer_connections:
                    del self.peer_connections[peer_id]

    def update_commit_index(self):
        """Leader updates commit_index based on majority replication"""
        if self.state != NodeState.LEADER:
            return

        # Find highest N such that majority of match_index[i] >= N
        # and log[N].term == currentTerm
        for n in range(self.commit_index + 1, len(self.log)):
            # Only commit entries from current term (safety)
            if self.log[n].term != self.current_term:
                continue

            # Count how many servers have replicated this entry
            replicated_count = 1  # Leader has it
            for peer_id in self.match_index:
                if self.match_index[peer_id] >= n:
                    replicated_count += 1

            # Check if majority
            if replicated_count > (len(self.peers) + 1) // 2:
                old_commit = self.commit_index
                self.commit_index = n
                print(f"[Node {self.node_id}] Committed entries up to index {n}")
                self.apply_committed_entries()

    def apply_committed_entries(self):
        """Apply committed but not yet applied entries to state machine"""
        while self.last_applied < self.commit_index:
            self.last_applied += 1
            entry = self.log[self.last_applied]
            self.apply_to_state_machine(entry)

    # ==================== Client Interface ====================

    def submit_value(self, value: str) -> dict:
        """
        Client submits a value to be added to the log
        Only leader processes client requests
        """
        with self.lock:
            if self.state != NodeState.LEADER:
                return {
                    "success": False,
                    "leader_id": self.leader_id,
                    "message": f"Not leader. Current leader is Node {self.leader_id}"
                }

            # Create new log entry
            print(f"[Node {self.node_id}] DEBUG: Before adding entry, log_length={len(self.log)}")
            new_entry = LogEntry(
                term=self.current_term,
                index=len(self.log),
                value=value
            )
            self.log.append(new_entry)
            self.save_state()

            print(f"[Node {self.node_id}] Client submitted value '{value}' at index {new_entry.index}, log_length={len(self.log)}")

        # Immediately replicate to followers
        self.send_heartbeats()

        # Wait for commit (simplified - poll for commit)
        max_wait = 5.0
        start_time = time.time()
        while time.time() - start_time < max_wait:
            with self.lock:
                if self.commit_index >= new_entry.index:
                    return {
                        "success": True,
                        "message": "Value committed",
                        "index": new_entry.index
                    }
            time.sleep(0.05)

        return {
            "success": False,
            "message": "Timeout waiting for commit"
        }

    def get_status(self) -> dict:
        """Get current node status (for monitoring)"""
        with self.lock:
            return {
                "node_id": self.node_id,
                "state": self.state.value,
                "term": self.current_term,
                "leader_id": self.leader_id,
                "log_length": len(self.log),
                "commit_index": self.commit_index,
                "last_applied": self.last_applied
            }

    def stepdown(self):
        """Force leader to step down (for testing scenario b)"""
        with self.lock:
            if self.state == NodeState.LEADER:
                print(f"[Node {self.node_id}] Stepping down as leader")
                self.state = NodeState.FOLLOWER
                self.leader_id = None
                self.voted_for = None
                self.pause_replication = False  # Clear pause flag when stepping down
                self.last_heartbeat = time.time()  # Reset election timeout
                self.save_state()
                return {"success": True}
            return {"success": False, "message": "Not a leader"}

    def add_uncommitted_entry(self, value: str) -> dict:
        """
        TEST ONLY: Add entry to leader's log without replicating
        Used for Scenario C to create inconsistent log state
        """
        with self.lock:
            if self.state != NodeState.LEADER:
                return {
                    "success": False,
                    "message": "Not leader"
                }

            # Create new log entry (but don't replicate or commit)
            print(f"[Node {self.node_id}] DEBUG: Before adding uncommitted entry, log_length={len(self.log)}")

            # Pause replication to prevent heartbeat timer from sending this entry
            self.pause_replication = True

            new_entry = LogEntry(
                term=self.current_term,
                index=len(self.log),
                value=value
            )
            self.log.append(new_entry)
            self.save_state()

            print(f"[Node {self.node_id}] TEST: Added uncommitted entry '{value}' at index {new_entry.index}, log_length={len(self.log)}")
            print(f"[Node {self.node_id}] WARNING: Entry NOT replicated - will be lost if leader crashes!")
            print(f"[Node {self.node_id}] WARNING: Replication PAUSED - heartbeats suspended until crash/restart")

            return {
                "success": True,
                "message": "Uncommitted entry added (TEST ONLY)",
                "index": new_entry.index
            }

    # ==================== Cluster Initialization ====================

    def wait_for_cluster_ready(self, timeout: float = 60.0) -> bool:
        """
        Wait for all peer nodes to be reachable before starting election.
        This ensures all nodes are up before the cluster begins operation.

        Returns True if all peers become reachable within timeout, False otherwise.
        """
        if not self.wait_for_cluster:
            print(f"[Node {self.node_id}] Cluster ready check disabled, starting immediately")
            return True

        print(f"[Node {self.node_id}] Waiting for all peer nodes to be reachable...")
        print(f"[Node {self.node_id}] Expected peers: {[f'Node {pid}' for pid, _, _ in self.peers]}")

        start_time = time.time()
        reachable_peers = set()

        while time.time() - start_time < timeout:
            # Try to connect to each peer
            for peer_id, host, port in self.peers:
                if peer_id in reachable_peers:
                    continue  # Already confirmed reachable

                try:
                    # Attempt connection with short timeout
                    connection = Client((host, port), authkey=b'raft_cluster')
                    proxy = RPCProxy(connection)

                    # Quick health check - just get status
                    status = proxy.get_status()
                    if status:
                        reachable_peers.add(peer_id)
                        print(f"[Node {self.node_id}] ✓ Node {peer_id} is reachable ({host}:{port})")

                        # Cache the connection
                        self.peer_connections[peer_id] = proxy

                except Exception as e:
                    # Peer not ready yet, will retry
                    pass

            # Check if all peers are reachable
            if len(reachable_peers) == len(self.peers):
                print(f"[Node {self.node_id}] ✓ All {len(self.peers)} peer nodes are reachable!")
                print(f"[Node {self.node_id}] Cluster is ready, starting election timer...")
                self.cluster_ready = True
                return True

            # Show progress
            if int(time.time() - start_time) % 5 == 0:  # Every 5 seconds
                missing = len(self.peers) - len(reachable_peers)
                print(f"[Node {self.node_id}] Still waiting... ({len(reachable_peers)}/{len(self.peers)} peers reachable, {missing} pending)")

            time.sleep(1.0)  # Wait before retrying

        # Timeout reached
        print(f"[Node {self.node_id}] ⚠ Warning: Timeout waiting for all peers!")
        print(f"[Node {self.node_id}] Reachable: {len(reachable_peers)}/{len(self.peers)} peers")
        print(f"[Node {self.node_id}] Starting anyway to allow partial cluster operation...")
        self.cluster_ready = True  # Start anyway
        return False

    # ==================== RPC Connection Management ====================

    def get_peer_connection(self, peer_id: int) -> Optional[RPCProxy]:
        """Get or create RPC connection to a peer"""
        if peer_id in self.peer_connections:
            return self.peer_connections[peer_id]

        # Find peer info
        peer_info = None
        for pid, host, port in self.peers:
            if pid == peer_id:
                peer_info = (host, port)
                break

        if peer_info is None:
            print(f"[Node {self.node_id}] ERROR: Peer {peer_id} not found in peer list")
            return None

        # Try to connect
        try:
            print(f"[Node {self.node_id}] Attempting to connect to Node {peer_id} at {peer_info[0]}:{peer_info[1]}")
            connection = Client(peer_info, authkey=b'raft_cluster')
            proxy = RPCProxy(connection)
            self.peer_connections[peer_id] = proxy
            print(f"[Node {self.node_id}] Successfully connected to Node {peer_id}")
            return proxy
        except Exception as e:
            print(f"[Node {self.node_id}] ERROR connecting to Node {peer_id} at {peer_info[0]}:{peer_info[1]}: {e}")
            return None

    # ==================== Main Loop & Timers ====================

    def run(self):
        """Start the Raft node - Phase 1: Initialize without starting timers"""
        # Load persistent state
        self.load_state()

        # Create log file if not exists
        if not os.path.exists(self.log_file):
            open(self.log_file, 'a').close()

        print(f"[Node {self.node_id}] Started successfully")
        print(f"[Node {self.node_id}] Election timeout: {self.election_timeout:.2f}s")

    def start_cluster_sync(self):
        """Start the Raft node - Phase 2: Wait for cluster and start timers"""
        # Wait for all peer nodes to be ready (if enabled)
        if self.wait_for_cluster:
            print(f"[Node {self.node_id}] Cluster synchronization enabled")
            self.wait_for_cluster_ready(timeout=60.0)
        else:
            print(f"[Node {self.node_id}] Starting immediately (cluster sync disabled)")
            self.cluster_ready = True

        # Start timers after cluster is ready
        self.running = True
        self.election_timer_thread = threading.Thread(target=self.election_timer, daemon=True)
        self.heartbeat_timer_thread = threading.Thread(target=self.heartbeat_timer, daemon=True)

        self.election_timer_thread.start()
        self.heartbeat_timer_thread.start()

        print(f"[Node {self.node_id}] Election and heartbeat timers started")
        print(f"[Node {self.node_id}] Ready for leader election or heartbeat...")

    def stop(self):
        """Stop the Raft node"""
        self.running = False
        print(f"[Node {self.node_id}] Stopped")

    def election_timer(self):
        """Background thread checking for election timeout"""
        while self.running:
            time.sleep(0.01)  # Check every 10ms

            should_start_election = False
            with self.lock:
                # Only followers and candidates can timeout
                if self.state != NodeState.LEADER:
                    elapsed = time.time() - self.last_heartbeat
                    if elapsed >= self.election_timeout:
                        # Election timeout - start new election
                        print(f"[Node {self.node_id}] Election timeout ({self.election_timeout:.2f}s elapsed)")
                        should_start_election = True

            # Call start_election outside the lock to avoid deadlock
            if should_start_election:
                self.start_election()

    def heartbeat_timer(self):
        """Background thread for leader to send heartbeats"""
        while self.running:
            time.sleep(self.heartbeat_interval)

            with self.lock:
                if self.state == NodeState.LEADER:
                    self.send_heartbeats()
