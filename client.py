# -*- coding:utf-8 -*-
"""
CISC 6935 Distributed Systems - Lab 2
Client - Run this on client node to test Raft cluster
"""

import time
import os
from multiprocessing.connection import Client
import pickle


class RPCProxy:
    """Proxy for making RPC calls to Raft nodes"""
    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        def do_rpc(*args, **kwargs):
            try:
                self._connection.send(pickle.dumps((name, args, kwargs)))
                result = pickle.loads(self._connection.recv())
                if isinstance(result, Exception):
                    raise result
                return result
            except Exception as e:
                raise ConnectionError(f"RPC failed: {str(e)}")
        return do_rpc


class RaftClient:
    """Client for testing Raft cluster"""

    def __init__(self):
        # GCP cluster configuration
        self.nodes = [
            ('10.128.0.2', 5000),  # node1
            ('10.128.0.3', 5000),  # node2
            ('10.128.0.4', 5000)   # node3
        ]

    def connect_to_node(self, node_id: int):
        """Create RPC connection to a node"""
        try:
            host, port = self.nodes[node_id]
            connection = Client((host, port), authkey=b'raft_cluster')
            return RPCProxy(connection)
        except Exception as e:
            print(f"Failed to connect to Node {node_id}: {e}")
            return None

    def find_leader(self, timeout=10) -> int:
        """Find the current leader"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            for i in range(len(self.nodes)):
                try:
                    proxy = self.connect_to_node(i)
                    if proxy:
                        status = proxy.get_status()
                        if status['state'] == 'leader':
                            return i
                except Exception:
                    pass
            time.sleep(0.5)
        raise Exception("No leader found within timeout!")

    def get_node_status(self, node_id: int) -> dict:
        """Get status of a node"""
        try:
            proxy = self.connect_to_node(node_id)
            if proxy:
                return proxy.get_status()
        except Exception as e:
            print(f"Failed to get status of Node {node_id}: {e}")
        return None

    def submit_value(self, node_id: int, value: str):
        """Submit a value to a specific node"""
        try:
            proxy = self.connect_to_node(node_id)
            if proxy:
                result = proxy.submit_value(value)
                return result
        except Exception as e:
            print(f"Failed to submit value to Node {node_id}: {e}")
            return {"success": False, "error": str(e)}

    def read_log_file(self, node_id: int) -> str:
        """Read the CISC6935 log file of a node"""
        log_file = f"CISC6935_node{node_id}.log"
        if os.path.exists(log_file):
            with open(log_file, 'r') as f:
                return f.read()
        return ""

    def stepdown_leader(self, leader_id: int):
        """Force leader to step down"""
        try:
            proxy = self.connect_to_node(leader_id)
            if proxy:
                return proxy.stepdown()
        except Exception as e:
            print(f"Failed to step down Node {leader_id}: {e}")
            return {"success": False}

    def add_uncommitted_entry(self, node_id: int, value: str):
        """Add uncommitted entry to leader (TEST ONLY - for Scenario C)"""
        try:
            proxy = self.connect_to_node(node_id)
            if proxy:
                return proxy.add_uncommitted_entry(value)
        except Exception as e:
            print(f"Failed to add uncommitted entry to Node {node_id}: {e}")
            return {"success": False}

    def verify_clean_cluster(self) -> bool:
        """Verify all nodes have clean state (no log entries)"""
        print("  Verifying cluster has clean state...")

        for i in range(len(self.nodes)):
            status = self.get_node_status(i)
            if status:
                if status['log_length'] != 0:
                    print(f"  ✗ Node {i} has existing log ({status['log_length']} entries)")
                    return False
                if status['commit_index'] != -1:
                    print(f"  ✗ Node {i} has commit_index={status['commit_index']} (should be -1)")
                    return False

        print("  ✓ All nodes have clean state (no log entries)")
        return True

    def check_cluster_status(self):
        """Check status of all nodes"""
        print("=" * 60)
        print("CLUSTER STATUS")
        print("=" * 60)

        leader_found = False
        for i in range(len(self.nodes)):
            host, port = self.nodes[i]
            print(f"\nNode {i} ({host}:{port}):")
            status = self.get_node_status(i)
            if status:
                state = status['state']
                term = status['term']
                log_len = status['log_length']
                commit = status['commit_index']

                if state == 'leader':
                    print(f"  ✓ LEADER (term {term}, log {log_len}, committed {commit})")
                    leader_found = True
                else:
                    print(f"  ✓ {state.upper()} (term {term}, log {log_len}, committed {commit})")
            else:
                print(f"  ✗ NOT REACHABLE")

        print("\n" + "=" * 60)
        if leader_found:
            print("✓ CLUSTER READY - Leader found!")
        else:
            print("✗ NO LEADER - Check if all servers are running")
        print("=" * 60)

        return leader_found

    # ==================== Test Scenarios ====================

    def test_scenario_a(self):
        """
        Scenario A (50%): Basic Leader Election
        """
        print("\n" + "=" * 70)
        print("SCENARIO A: Basic Leader Election (50%)")
        print("=" * 70)

        print("\n💡 TIP: For clean testing, remove old state files:")
        print("  Run on all nodes: rm -f raft_state_node*.json CISC6935_node*.log")
        print("  Then restart all nodes: python3 server.py <node_id>")
        print("\nPress Enter to continue with test...")
        input()

        # Step 1: Wait for leader election
        print("\n[Step 1] Waiting for leader election...")
        time.sleep(2)

        try:
            leader_id = self.find_leader()
            print(f"✓ Leader elected: Node {leader_id}")
        except Exception as e:
            print(f"✗ Failed to elect leader: {e}")
            return False

        # Step 2: Submit values through leader
        print("\n[Step 2] Submitting 5 values through leader...")
        for i in range(1, 6):
            value = str(i)
            result = self.submit_value(leader_id, value)
            if result.get('success'):
                print(f"  ✓ Submitted and committed: {value}")
            else:
                print(f"  ✗ Failed to submit {value}: {result}")
            time.sleep(0.5)

        # Step 3: Wait for replication
        print("\n[Step 3] Waiting for log replication...")
        time.sleep(3)

        # Step 4: Check all node status
        print("\n[Step 4] Checking all nodes...")
        for i in range(len(self.nodes)):
            status = self.get_node_status(i)
            if status:
                print(f"  Node {i}: {status['state']}, log={status['log_length']}, committed={status['commit_index']}")

        print("\n" + "=" * 70)
        print("SCENARIO A: COMPLETED ✓")
        print("=" * 70)
        return True

    def test_scenario_b(self):
        """
        Scenario B (10%): Leader Change (Perfect Situation)
        """
        print("\n" + "=" * 70)
        print("SCENARIO B: Leader Change - Perfect Situation (10%)")
        print("=" * 70)

        print("\n💡 TIP: For clean testing, remove old state files:")
        print("  Run on all nodes: rm -f raft_state_node*.json CISC6935_node*.log")
        print("  Then restart all nodes: python3 server.py <node_id>")
        print("\nPress Enter to continue with test...")
        input()

        # Step 1: Find current leader
        print("\n[Step 1] Finding current leader...")
        try:
            old_leader = self.find_leader()
            print(f"✓ Current leader: Node {old_leader}")
        except Exception as e:
            print(f"✗ No leader found: {e}")
            return False

        # Step 2: Submit some values
        print("\n[Step 2] Submitting values 1-3 through current leader...")
        for i in range(1, 4):
            value = str(i)
            result = self.submit_value(old_leader, value)
            if result.get('success'):
                print(f"  ✓ Submitted: {value}")
            else:
                print(f"  ✗ Failed: {value}")
            time.sleep(0.5)

        # Step 2.5: Wait for FULL replication (perfect situation)
        print("\n[Step 2.5] Waiting for FULL replication to all nodes (perfect situation)...")

        # Poll until all nodes have identical logs (with timeout)
        max_wait = 10.0
        start_time = time.time()
        perfect_replication = False

        while time.time() - start_time < max_wait:
            statuses = []
            all_reachable = True

            for i in range(len(self.nodes)):
                status = self.get_node_status(i)
                if status:
                    statuses.append(status)
                else:
                    all_reachable = False
                    break

            if all_reachable and len(statuses) == len(self.nodes):
                # Check if all nodes have same log_length and commit_index
                log_lengths = [s['log_length'] for s in statuses]
                commit_indices = [s['commit_index'] for s in statuses]

                if len(set(log_lengths)) == 1 and len(set(commit_indices)) == 1:
                    # All nodes have identical logs
                    perfect_replication = True
                    print(f"  ✓ All nodes synchronized: log_length={log_lengths[0]}, commit_index={commit_indices[0]}")
                    break

            time.sleep(0.5)

        if not perfect_replication:
            print("  ✗ WARNING: Failed to achieve perfect replication within timeout!")
            print("  Verifying current state:")
            for i in range(len(self.nodes)):
                status = self.get_node_status(i)
                if status:
                    print(f"    Node {i}: log_length={status['log_length']}, commit_index={status['commit_index']}")
            return False

        print("  ✓ Perfect situation confirmed - all logs fully replicated")

        # Step 3: Leader steps down gracefully
        print(f"\n[Step 3] Forcing Node {old_leader} to step down gracefully...")
        self.stepdown_leader(old_leader)
        time.sleep(2)

        # Step 4: Find new leader
        print("\n[Step 4] Waiting for new leader election...")
        try:
            new_leader = self.find_leader()
            print(f"✓ New leader elected: Node {new_leader}")
            if new_leader == old_leader:
                print("  ⚠ Warning: Same node became leader again")
        except Exception as e:
            print(f"✗ Failed to elect new leader: {e}")
            return False

        # Step 5: Submit more values
        print("\n[Step 5] Submitting values 4-6 through new leader...")
        for i in range(4, 7):
            value = str(i)
            result = self.submit_value(new_leader, value)
            if result.get('success'):
                print(f"  ✓ Submitted: {value}")
            else:
                print(f"  ✗ Failed: {value}")
            time.sleep(0.5)

        # Step 6: Wait and verify
        print("\n[Step 6] Waiting for log replication...")
        time.sleep(3)

        print("\n[Step 7] Final status:")
        for i in range(len(self.nodes)):
            status = self.get_node_status(i)
            if status:
                print(f"  Node {i}: log={status['log_length']}, committed={status['commit_index']}")

        print("\n" + "=" * 70)
        print("SCENARIO B: COMPLETED ✓")
        print("=" * 70)
        return True

    def test_scenario_c_interactive(self):
        """
        Scenario C (10%): Leader Crash with Inconsistent Log
        Note: Requires manual node crash (Ctrl+C on one node)
        """
        print("\n" + "=" * 70)
        print("SCENARIO C: Leader Crash with Inconsistent Log (10%)")
        print("=" * 70)

        print("\n⚠ IMPORTANT: This test REQUIRES a clean cluster state!")
        print("\n  To clean and restart:")
        print("  1. On EACH node (node1, node2, node3), run:")
        print("     rm -f raft_state_node*.json CISC6935_node*.log")
        print("  2. RESTART all nodes:")
        print("     python3 server.py 0")
        print("     python3 server.py 1")
        print("     python3 server.py 2")
        print("  3. Wait for cluster to sync (all nodes reachable)")
        print("\nPress Enter when you've completed cleanup and restart...")
        input()

        # Step 0: Verify clean state
        print("\n[Step 0] Verifying cluster has clean state...")
        if not self.verify_clean_cluster():
            print("\n✗ ERROR: Cluster is not clean!")
            print("  Some nodes still have log entries from previous tests.")
            print("\n  Please follow these steps:")
            print("  1. Stop all nodes (Ctrl+C on each terminal)")
            print("  2. Delete state files on ALL nodes:")
            print("     rm -f raft_state_node*.json CISC6935_node*.log")
            print("  3. Restart all nodes:")
            print("     python3 server.py 0")
            print("     python3 server.py 1")
            print("     python3 server.py 2")
            print("  4. Run this test again")
            return False

        print("✓ Cluster state is clean, proceeding with test...")

        # Step 1: Find current leader and record initial state
        print("\n[Step 1] Finding current leader and recording initial state...")
        try:
            leader = self.find_leader()
            print(f"✓ Current leader: Node {leader}")

            # Record initial log state (should be 0 since we verified clean state)
            initial_status = self.get_node_status(leader)
            if initial_status:
                initial_log_length = initial_status['log_length']
                print(f"  Initial log length: {initial_log_length}")

                # Double-check it's really clean
                if initial_log_length != 0:
                    print(f"✗ ERROR: Log is not empty! Something went wrong.")
                    return False
            else:
                print(f"✗ Failed to get initial status")
                return False
        except Exception as e:
            print(f"✗ No leader found: {e}")
            return False

        # Step 2: Submit and commit some values
        print("\n[Step 2] Submitting and committing values 1-2...")
        for i in range(1, 3):
            result = self.submit_value(leader, str(i))
            if result.get('success'):
                print(f"  ✓ Submitted and committed: {i}")
            time.sleep(0.5)

        print("  Waiting for full replication...")
        time.sleep(2)

        # Verify all nodes have same log before creating inconsistency
        print("\n  Verifying all nodes have consistent logs (values 1-2)...")
        for i in range(len(self.nodes)):
            status = self.get_node_status(i)
            if status:
                print(f"    Node {i}: log_length={status['log_length']}, commit_index={status['commit_index']}")

        # Step 2.5: Create inconsistent log by adding uncommitted entry to leader
        print(f"\n[Step 2.5] Creating inconsistent log scenario...")
        print(f"  Adding value '3' to leader's log WITHOUT replication...")
        result = self.add_uncommitted_entry(leader, "3")
        if result.get('success'):
            print(f"  ✓ Value '3' added to Node {leader} (uncommitted, NOT replicated)")
            print(f"  → Leader has entry that followers DON'T have (inconsistent log)")
        else:
            print(f"  ✗ Failed to create inconsistency: {result}")
            return False

        # Verify inconsistency
        print("\n  Verifying inconsistent state:")
        for i in range(len(self.nodes)):
            status = self.get_node_status(i)
            if status:
                # Leader should have +3 entries (initial + 2 committed + 1 uncommitted)
                # Followers should have +2 entries (initial + 2 committed)
                expected = initial_log_length + 3 if i == leader else initial_log_length + 2
                actual = status['log_length']
                marker = "✓" if actual == expected else "✗"
                print(f"    Node {i}: log_length={actual} (expected {expected}) {marker}")

        # Step 3: Manual crash instruction
        print(f"\n[Step 3] MANUAL ACTION REQUIRED:")
        print(f"  ⚠ Leader (Node {leader}) now has an uncommitted entry that followers don't have")
        print(f"  1. On the terminal running Node {leader} (the current leader),")
        print(f"     press Ctrl+C to crash it NOW")
        print(f"  2. Press Enter here when you've crashed Node {leader}...")
        input()

        # Step 4: Wait for new election
        print("\n[Step 4] Waiting for new leader election...")
        time.sleep(3)

        try:
            new_leader = self.find_leader()
            print(f"✓ New leader elected: Node {new_leader}")
        except Exception as e:
            print(f"✗ Failed to elect new leader: {e}")
            return False

        # Step 4.5: Verify uncommitted entry was discarded
        print("\n[Step 4.5] Verifying uncommitted entry from crashed leader was discarded...")
        print(f"  → The old leader's uncommitted value '3' should be LOST")
        print(f"  → Remaining nodes should have consistent logs")

        time.sleep(1)
        expected_log_after_discard = initial_log_length + 2  # Initial + 2 committed values
        expected_commit = expected_log_after_discard - 1     # commit_index is 0-based

        for i in range(len(self.nodes)):
            if i == leader:
                continue  # Skip crashed node
            status = self.get_node_status(i)
            if status:
                if status['log_length'] == expected_log_after_discard and status['commit_index'] == expected_commit:
                    print(f"    Node {i}: log_length={status['log_length']}, commit_index={status['commit_index']} ✓ (correct)")
                else:
                    print(f"    Node {i}: log_length={status['log_length']}, commit_index={status['commit_index']} ⚠")

        # Step 5: Submit more values
        print("\n[Step 5] Submitting NEW values 3-4 through new leader...")
        print(f"  → These are NEW entries from term {new_leader}'s term, replacing the lost entry")
        for i in range(3, 5):
            result = self.submit_value(new_leader, str(i))
            if result.get('success'):
                print(f"  ✓ Submitted: {i}")
            time.sleep(0.5)

        # Step 6: Bonus - restart crashed node
        print(f"\n[Step 6] MANUAL ACTION (Bonus - Scenario D):")
        print(f"  1. On node{leader+1}, restart the server:")
        print(f"     cd ~/Advanced_Distributed_Systems_Lab2")
        print(f"     python3 server.py {leader}")
        print(f"  2. Press Enter here when you've restarted Node {leader}...")
        input()

        # Step 7: Wait for sync
        print("\n[Step 7] Waiting for recovered node to sync...")
        print(f"  → Node {leader} should DELETE its old uncommitted entry")
        print(f"  → Node {leader} should SYNC with new leader (values 1-4)")
        time.sleep(5)

        # Step 8: Check final status and verify consistency
        print("\n[Step 8] Final cluster status and verification:")
        print(f"  Verifying ALL nodes now have consistent logs...")

        all_consistent = True
        # Expected: initial + 2 (committed before crash) + 2 (new values) = initial + 4
        expected_log_length = initial_log_length + 4
        expected_commit_index = expected_log_length - 1  # 0-based index

        print(f"  Expected final state: log_length={expected_log_length}, commit_index={expected_commit_index}")

        for i in range(len(self.nodes)):
            status = self.get_node_status(i)
            if status:
                log_len = status['log_length']
                commit_idx = status['commit_index']
                is_correct = (log_len == expected_log_length and commit_idx == expected_commit_index)
                marker = "✓" if is_correct else "✗"

                print(f"  Node {i}: {status['state']}, log={log_len}, committed={commit_idx} {marker}")

                if not is_correct:
                    all_consistent = False
            else:
                print(f"  Node {i}: NOT REACHABLE ✗")
                all_consistent = False

        print("\n" + "=" * 70)
        if all_consistent:
            print("SCENARIO C: COMPLETED ✓")
            print("✓ All nodes have consistent logs!")
            print("✓ Crashed leader's uncommitted entry was discarded!")
            print("✓ Recovered node successfully synced with cluster!")
        else:
            print("SCENARIO C: COMPLETED with WARNINGS ⚠")
            print("⚠ Some nodes have inconsistent logs")
        print("=" * 70)
        return True

    def run_all_tests(self):
        """Run all test scenarios"""
        print("=" * 70)
        print("RAFT DISTRIBUTED FILE SYSTEM - TEST SUITE")
        print("=" * 70)

        # Check cluster first
        if not self.check_cluster_status():
            print("\n✗ Cluster not ready. Please ensure all 3 servers are running:")
            print("  node1: python3 server.py 0")
            print("  node2: python3 server.py 1")
            print("  node3: python3 server.py 2")
            return

        print("\nPress Enter to run Scenario A...")
        input()
        self.test_scenario_a()

        print("\n\nPress Enter to run Scenario B...")
        input()
        self.test_scenario_b()

        print("\n\nPress Enter to run Scenario C (requires manual node crash)...")
        input()
        self.test_scenario_c_interactive()

        print("\n\n" + "=" * 70)
        print("ALL TESTS COMPLETED!")
        print("=" * 70)


if __name__ == '__main__':
    client = RaftClient()

    # Check cluster status first
    client.check_cluster_status()

    print("\nOptions:")
    print("  1. Run all tests (scenarios A, B, C)")
    print("  2. Check cluster status only")
    print("  3. Interactive mode (manual commands)")

    choice = input("\nEnter choice (1/2/3): ").strip()

    if choice == '1':
        client.run_all_tests()
    elif choice == '2':
        pass  # Already showed status above
    elif choice == '3':
        print("\nInteractive mode - enter commands:")
        print("  status - show cluster status")
        print("  submit <value> - submit value to leader")
        print("  leader - find current leader")
        print("  quit - exit")

        while True:
            cmd = input("\n> ").strip().split()
            if not cmd:
                continue

            if cmd[0] == 'quit':
                break
            elif cmd[0] == 'status':
                client.check_cluster_status()
            elif cmd[0] == 'leader':
                try:
                    leader = client.find_leader()
                    print(f"Current leader: Node {leader}")
                except Exception as e:
                    print(f"No leader found: {e}")
            elif cmd[0] == 'submit' and len(cmd) > 1:
                try:
                    leader = client.find_leader()
                    result = client.submit_value(leader, cmd[1])
                    print(f"Result: {result}")
                except Exception as e:
                    print(f"Error: {e}")
            else:
                print("Unknown command")
