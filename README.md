# CISC 6935 Lab 2: Raft Consensus Algorithm

A distributed file system implementation using the Raft consensus algorithm for maintaining replicated log files (CISC6935_node{0,1,2}.log) across 3 nodes.

## Files

- **server.py** - Raft server (run on each of the 3 Raft nodes)
- **client.py** - Test client for all scenarios (run on client node)
- **raft_node.py** - Core Raft implementation (leader election, log replication, persistence)
- **cleanup.sh** - Cleanup script to reset all persistent state (run before each test)

## Quick Start (Clean Environment)

**IMPORTANT**: Before running tests, clean old state files:

```bash
# On each Raft node (node1, node2, node3), run:
./cleanup.sh

# OR manually:
rm -f raft_state_node*.json CISC6935_node*.log
```

This prevents issues with old term numbers and log entries from previous test runs.

## Deployment

### On Raft Nodes (node1, node2, node3)

Start one server on each node. **By default**, each node will wait for all other nodes to be reachable before starting the election process:

```bash
# On node1 (10.128.0.2)
python3 server.py 0

# On node2 (10.128.0.3)
python3 server.py 1

# On node3 (10.128.0.4)
python3 server.py 2
```

**Expected output:**
```
[Node 0] Waiting for all peer nodes to be reachable...
[Node 0] Expected peers: ['Node 1', 'Node 2']
[Node 0] ✓ Node 1 is reachable (10.128.0.3:5000)
[Node 0] ✓ Node 2 is reachable (10.128.0.4:5000)
[Node 0] ✓ All 2 peer nodes are reachable!
[Node 0] Cluster is ready, starting election timer...
[Node 0] *** BECAME LEADER for term 1 ***
```

**Alternative: Skip waiting (for testing):**

If you want nodes to start immediately without waiting:

```bash
python3 server.py 0 --no-wait
python3 server.py 1 --no-wait
python3 server.py 2 --no-wait
```

### On Client Node (node0)

Run the test client:

```bash
python3 client.py
```

Select option:
- **1** - Run all test scenarios automatically
- **2** - Check cluster status only
- **3** - Interactive mode (manual commands)

## Test Scenarios

### Scenario A (50%): Leader Election - Perfect Conditions
- No crashes, no slow nodes
- Submits 5 values (1-5) through leader
- Verifies log consistency across all 3 nodes

### Scenario B (10%): Leader Change - Perfect Situation
- Submits values 1-3, waits for FULL replication to all nodes
- Leader gracefully steps down (no crash)
- New leader elected
- Submits values 4-6 through new leader
- Verifies final consistency

**Perfect Situation**: All entries fully replicated to ALL nodes before leader change (no partial replication)

### Scenario C (10%): Leader Crash - Inconsistent Log
- Submits values 1-2, fully replicated
- **Manual action**: User crashes current leader (Ctrl+C)
- New leader elected from remaining followers
- Submits values 3-4 through new leader
  
### Bonus (Scenario D): User manually restarts crashed node
- Crashed node rejoins and syncs with current leader

**Inconsistency**: Leader has uncommitted entry that followers don't have (simulates crash before replication)

### Network Delay Simulation (Requirement E)
- Election timeout: randomized 150-300ms
- Heartbeat interval: 50ms
- Simulates realistic network conditions

## Key Features

### Raft Implementation
- **Leader Election**: Randomized timeouts, majority voting
- **Log Replication**: AppendEntries RPC with consistency checks
- **Persistence**: Saves current_term, voted_for, log to disk (raft_state_nodeX.json)
- **State Machine**: Applies committed entries to CISC6935_nodeX.log
- **Cluster Synchronization**: Waits for all nodes to be ready before starting elections (prevents split-brain during startup)

### RPC Framework
- Uses `multiprocessing.connection` (from Lab 1)
- Listens on `0.0.0.0:5000` for GCP internal networking
- Thread-safe connection handling

## Configuration

Edit `server.py` to change cluster IPs:

```python
cluster_config = [
    (0, '10.128.0.2', 5000),  # node1
    (1, '10.128.0.3', 5000),  # node2
    (2, '10.128.0.4', 5000)   # node3
]
```

Edit `client.py` with same IPs.

## Troubleshooting

**No leader found**: Ensure all 3 servers are running and can communicate.

**Connection refused**: Check firewall allows port 5000:
```bash
gcloud compute firewall-rules create allow-raft-internal \
  --allow tcp:5000 \
  --source-ranges 10.128.0.0/20
```

**Nodes not communicating**: Check server output for connection errors.

**Node stuck after restart with high term number** (e.g., `Loaded state: term=297`):
- Old persistent state is preventing new elections
- Clean state files before restarting:
```bash
./cleanup.sh
# OR manually:
rm -f raft_state_node*.json CISC6935_node*.log
```
- Then restart all nodes

**Tests showing wrong expected values**: Run cleanup script before each test scenario to start with fresh state.

## Architecture

```
Client (node0)
    ↓
Raft Cluster:
  - Node 0 (node1:10.128.0.2:5000)
  - Node 1 (node2:10.128.0.3:5000)  ← Leader elected
  - Node 2 (node3:10.128.0.4:5000)
    ↓
Replicated Log Files:
  - CISC6935_node0.log
  - CISC6935_node1.log
  - CISC6935_node2.log
```

## Implementation Highlights

- **Cluster synchronization**: Nodes wait for all peers before starting elections, ensuring coordinated startup
- **Deadlock prevention**: Election timer calls start_election() outside lock
- **Graceful stepdown**: Leader can voluntarily convert to follower for Scenario B
- **Log inconsistency handling**: Uncommitted entries overwritten on leader change
- **Crash recovery**: Nodes reload state from disk and sync with current leader
- **Network delay simulation**: Random 0-50ms delay on all RPC calls
