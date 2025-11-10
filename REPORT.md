# CISC 6935 Lab 2: Raft Consensus Algorithm Implementation Report

## Project Overview

This project implements a distributed file system using the Raft consensus algorithm to maintain replicated log files across a 3-node cluster. The system ensures strong consistency through leader election, log replication, and crash recovery mechanisms.

## Architecture and Design Decisions

### System Architecture

The implementation consists of three main components:

1. **RaftNode (raft_node.py)**: Core Raft protocol implementation
2. **Server (server.py)**: RPC server wrapper for each node
3. **Client (client.py)**: Test client for scenario validation

**Key Design Choice - RPC Framework**: I chose to use Python's `multiprocessing.connection` module for RPC communication (carried over from Lab 1) because:
- Provides authenticated connections with built-in serialization
- Simple request-response pattern suitable for Raft RPCs
- Thread-safe for handling concurrent connections

**Network Configuration**: Nodes listen on `0.0.0.0:5000` to accept connections from any interface, essential for GCP's internal networking where nodes communicate via private IPs.

### Cluster Synchronization Strategy

**Design Problem**: When starting the cluster, nodes would immediately begin elections before all peers were reachable, causing split-brain scenarios and election failures.

**Solution Implemented**: Added a cluster synchronization phase (raft_node.py:187-231):

```python
def wait_for_cluster_ready(self):
    """Wait for all peer nodes to be reachable before starting elections"""
    print(f"[Node {self.node_id}] Waiting for all peer nodes to be reachable...")
    
    while True:
        all_ready = True
        for peer_id, host, port in self.peers:
            if not self._check_peer_reachable(peer_id, host, port):
                all_ready = False
                time.sleep(1)
                break
        
        if all_ready:
            print(f"[Node {self.node_id}] ✓ All {len(self.peers)} peer nodes are reachable!")
            break
```

This ensures coordinated startup - all nodes wait until the full cluster is online before beginning the election process. This prevents wasted election attempts and ensures clean initial leader election.

### State Persistence Design

**Design Challenge**: Raft requires persistence of `currentTerm`, `votedFor`, and `log[]` to ensure correctness across crashes. How should this state be saved?

**Implementation Decision**: JSON-based persistence (raft_node.py:254-296):

```python
def save_persistent_state(self):
    """Save Raft state to disk"""
    state = {
        'current_term': self.current_term,
        'voted_for': self.voted_for,
        'log': self.log
    }
    with open(self.state_file, 'w') as f:
        json.dump(state, f, indent=2)
```

**Why JSON over pickle/binary?**
- Human-readable for debugging
- Safe across Python versions
- Easy to inspect state during development
- Sufficient performance for lab-scale cluster

**Critical Design Decision**: Save state **before** responding to RPCs. In `request_vote()`, I save state immediately after updating `currentTerm` or `votedFor`, ensuring durability before acknowledging the vote.

## Major Implementation Challenges

### Challenge 1: Critical Deadlock Bug

**What was the problem?**

The system completely froze during leader election. Nodes would print "Election timeout" but never proceed - no RequestVote RPCs were sent, and no leader was elected.

**How did I identify it?**

Through systematic debugging:
1. Added extensive logging to track thread execution
2. Discovered that the `election_timer()` thread was blocking indefinitely
3. Analyzed lock acquisition patterns and found nested locking

**Root cause** (raft_node.py:814-831):

The election timer was calling `start_election()` while holding the global lock. But `start_election()` also needs to acquire the same lock - classic deadlock.

```python
# WRONG - Causes deadlock
def election_timer(self):
    with self.lock:  # Lock acquired
        if elapsed >= self.election_timeout:
            self.start_election()  # Tries to acquire same lock → DEADLOCK
```

**How did I overcome it?**

Redesigned the critical section to minimize lock holding time:

```python
# CORRECT - No deadlock (raft_node.py:814-831)
def election_timer(self):
    should_start_election = False
    with self.lock:
        # Only READ state inside lock
        if self.state != NodeState.LEADER:
            elapsed = time.time() - self.last_heartbeat
            if elapsed >= self.election_timeout:
                should_start_election = True

    # Call start_election OUTSIDE lock
    if should_start_election:
        self.start_election()
```

**Design Lesson**: Never call complex functions (that may acquire locks) while holding a lock. Keep critical sections minimal - only for reading/writing shared state.

### Challenge 2: Log Inconsistency and State Machine Sync

**The Challenge:**

For crash recovery (Scenario D), a crashed node needs to rejoin and sync its state. This revealed a subtle bug: when the Raft log is truncated due to conflicts, the state machine log (CISC6935_nodeX.log) was out of sync, causing duplicate or incorrect entries.

**The Problem Scenario:**
1. Leader has entries [1, 2, 3, 4] with commitIndex=2
2. Node crashes, leader commits entries 3, 4
3. Node restarts with log [1, 2, 3, 4] but commitIndex=2
4. New leader has conflicting entry at index 3
5. Follower truncates log to [1, 2] then appends new entries
6. But state machine log still has the old entries!

**Solution Implemented** (raft_node.py:698-732):

```python
def apply_committed_entries(self):
    """Apply committed log entries to state machine WITH truncation support"""
    with self.lock:
        while self.last_applied < self.commit_index:
            self.last_applied += 1
            entry = self.log[self.last_applied]
            
            # Critical: Rebuild state machine from scratch on log truncation
            if self.last_applied == 1:
                # First entry - clear existing state machine
                with open(self.log_file, 'w') as f:
                    f.write('')  # Clear file
            
            with open(self.log_file, 'a') as f:
                f.write(f"{entry['value']}\n")
```

**Key Insight**: When applying the first entry after a restart or log truncation, clear the state machine file completely. This ensures the state machine is rebuilt correctly from the Raft log, which is the single source of truth.

### Challenge 3: Leader Stepping Down Gracefully

**Design Question**: For Scenario B, the leader needs to step down voluntarily (not crash) and trigger a new election. How should this work?

**Initial Approach (Wrong)**:
```python
def stepdown(self):
    self.state = NodeState.FOLLOWER
    return True
```

This doesn't work because the node stays silent - no other node knows to start an election.

**Correct Implementation** (raft_node.py:784-808):

```python
def stepdown(self):
    """Gracefully step down as leader"""
    with self.lock:
        if self.state == NodeState.LEADER:
            print(f"\n[Node {self.node_id}] *** STEPPING DOWN as leader ***")
            self.state = NodeState.FOLLOWER
            self.leader_id = None
            self.voted_for = None
            self.save_persistent_state()
            
            # Reset election timeout to trigger election soon
            self.last_heartbeat = time.time() - (self.election_timeout * 0.9)
            
            return {'success': True, 'message': 'Leader stepped down'}
```

**Design Decision**: When stepping down, reset the `last_heartbeat` timestamp to almost trigger the election timeout. This causes the former leader to start a new election quickly, ensuring minimal downtime.

### Challenge 4: AppendEntries Consistency Check

**The Problem**: Raft's safety depends on the log consistency check in AppendEntries. If a follower's log doesn't match the leader's at `prevLogIndex`, it must reject the request.

**Implementation** (raft_node.py:577-641):

```python
def append_entries(self, term, leader_id, prev_log_index, prev_log_term, 
                  entries, leader_commit):
    with self.lock:
        # Reject if term is stale
        if term < self.current_term:
            return {'term': self.current_term, 'success': False}
        
        # Convert to follower if term is newer
        if term > self.current_term:
            self.current_term = term
            self.state = NodeState.FOLLOWER
            self.voted_for = None
            self.save_persistent_state()
        
        # Consistency check: prevLogIndex must exist and match prevLogTerm
        if prev_log_index > 0:
            if prev_log_index >= len(self.log):
                return {'term': self.current_term, 'success': False}
            if self.log[prev_log_index]['term'] != prev_log_term:
                # Conflict: truncate log
                self.log = self.log[:prev_log_index]
                self.save_persistent_state()
                return {'term': self.current_term, 'success': False}
        
        # Append new entries (may overwrite conflicts)
        # ... append logic ...
```

**Critical Design Point**: When a conflict is detected, truncate the log **immediately** and reject the request. Don't wait to append new entries - the leader will retry with an earlier prevLogIndex until finding the point where logs match.

## Testing Methodology

### Test Environment Setup

1. **Clean State Before Each Test**:
   ```bash
   ./cleanup.sh  # Removes raft_state_node*.json and CISC6935_node*.log
   ```
   This prevents contamination from previous test runs (old term numbers, stale log entries).

2. **Start Cluster**:
   ```bash
   # On each node (node1, node2, node3)
   python3 server.py 0  # node1
   python3 server.py 1  # node2
   python3 server.py 2  # node3
   ```

3. **Run Test Client**:
   ```bash
   # On client node (node0)
   python3 client.py
   # Select option 1 for automated test scenarios
   ```

### Test Scenarios

**Scenario A: Basic Leader Election and Log Replication**
- Tests: Normal operation with no failures
- Process: Submit values 1-5, verify all nodes have identical logs
- Validates: Leader election, log replication, commit mechanism

**Scenario B: Leader Change (Perfect Situation)**
- Tests: Graceful leader transition
- Process: Submit 1-3, wait for full replication, leader steps down, submit 4-6
- Validates: Graceful stepdown, new leader election, continued operation

**Scenario C: Leader Crash (Inconsistent Log)**
- Tests: Crash recovery with uncommitted entries
- Process: Submit 1-2, crash leader (manual Ctrl+C), new election, submit 3-4
- Validates: Log conflict resolution, consistency after crash

**Scenario D (Extra Credit): Crashed Node Rejoins**
- Tests: State machine reconstruction after rejoin
- Process: Restart crashed node, verify it syncs correctly
- Validates: State persistence, log replay, state machine consistency

### Verification Approach

For each scenario, the client:
1. Checks cluster status (who's the leader)
2. Submits values through the leader
3. Waits for commit (queries leader's commitIndex)
4. Reads log files from all nodes directly
5. Compares logs for consistency

Example verification output:
```
✓ Scenario A: PASSED
  Node 0 log: [1, 2, 3, 4, 5]
  Node 1 log: [1, 2, 3, 4, 5]
  Node 2 log: [1, 2, 3, 4, 5]
  All logs consistent!
```

## Key Implementation Details

### Thread Safety Strategy

The implementation uses a single global lock (`self.lock`) to protect all shared state:
- `current_term`, `voted_for`, `log[]`
- `commit_index`, `last_applied`
- `next_index[]`, `match_index[]`

**Pattern**: Acquire lock → Read/modify state → Release lock → Call external functions

This simple approach avoids complex locking hierarchies but requires careful design to prevent deadlocks (as learned in Challenge 1).

### Network Delay Simulation

To simulate realistic network conditions (Requirement E):

```python
def send_rpc(self, peer_id, func_name, *args, **kwargs):
    # Simulate network delay (0-50ms)
    time.sleep(random.uniform(0, 0.05))
    
    # Send RPC...
```

Election timeout is randomized (150-300ms) to prevent split votes.

### Heartbeat Mechanism

Leader sends empty AppendEntries (heartbeats) every 50ms:

```python
def heartbeat_loop(self):
    while self.running:
        if self.state == NodeState.LEADER:
            self.send_append_entries()
        time.sleep(0.05)  # 50ms interval
```

This prevents followers from timing out and starting unnecessary elections.

## Conclusion

The implementation successfully demonstrates all core Raft mechanisms:
- Leader election with split-vote prevention
- Log replication with consistency guarantees
- Crash recovery with state persistence
- State machine safety through log replay

The major challenges involved understanding concurrent programming pitfalls (deadlocks), ensuring state machine consistency during log truncation, and implementing proper leader transition mechanisms. Through systematic debugging and careful critical section design, all scenarios pass successfully.