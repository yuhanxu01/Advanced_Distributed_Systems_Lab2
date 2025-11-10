# -*- coding:utf-8 -*-
"""
CISC 6935 Distributed Systems - Lab 2
Raft Server - Run this on each Raft node
"""

import pickle
import sys
import threading
import time
from multiprocessing.connection import Listener
from raft_node import RaftNode


class RPCHandler:
    """Handles RPC requests for Raft node"""
    def __init__(self, raft_node: RaftNode):
        self.raft_node = raft_node
        self._functions = {}
        self._register_functions()

    def _register_functions(self):
        """Register all callable RPC functions"""
        self._functions['request_vote'] = self.raft_node.request_vote
        self._functions['append_entries'] = self.raft_node.append_entries
        self._functions['submit_value'] = self.raft_node.submit_value
        self._functions['get_status'] = self.raft_node.get_status
        self._functions['stepdown'] = self.raft_node.stepdown
        self._functions['add_uncommitted_entry'] = self.raft_node.add_uncommitted_entry

    def handle_connection(self, connection):
        """Handle incoming RPC connection"""
        try:
            while True:
                func_name, args, kwargs = pickle.loads(connection.recv())
                try:
                    result = self._functions[func_name](*args, **kwargs)
                    connection.send(pickle.dumps(result))
                except Exception as e:
                    connection.send(pickle.dumps(e))
        except EOFError:
            pass


def rpc_server(handler: RPCHandler, address: tuple, authkey: bytes, node_ip: str = None):
    """Start RPC server"""
    sock = Listener(address, authkey=authkey)
    if node_ip:
        print(f"[RPC Server] Listening on 0.0.0.0:{address[1]} (this node: {node_ip}:{address[1]})")
    else:
        print(f"[RPC Server] Listening on {address}")

    while True:
        client = sock.accept()
        t = threading.Thread(target=handler.handle_connection, args=(client,))
        t.daemon = True
        t.start()


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python server.py <node_id> [--no-wait]")
        print("  node_id: 0, 1, or 2")
        print("  --no-wait: (optional) Start immediately without waiting for other nodes")
        sys.exit(1)

    node_id = int(sys.argv[1])
    wait_for_cluster = True  # Default: wait for all nodes

    # Check for --no-wait flag
    if len(sys.argv) > 2 and sys.argv[2] == '--no-wait':
        wait_for_cluster = False
        print("⚠ Cluster synchronization disabled (--no-wait)")

    # Cluster configuration for GCP
    cluster_config = [
        (0, '10.128.0.2', 5000),  # node1
        (1, '10.128.0.3', 5000),  # node2
        (2, '10.128.0.4', 5000)   # node3
    ]

    # Get this node's config
    node_port = None
    node_ip = None
    peers = []

    for nid, host, port in cluster_config:
        if nid == node_id:
            node_port = port
            node_ip = host
        else:
            peers.append((nid, host, port))

    if node_port is None:
        print(f"Invalid node_id: {node_id}")
        sys.exit(1)

    print("=" * 60)
    print(f"Starting Raft Node {node_id}")
    print(f"This node: {node_ip}:{node_port}")
    if wait_for_cluster:
        print("Cluster sync: ENABLED (will wait for all peers)")
    else:
        print("Cluster sync: DISABLED (starting immediately)")
    print("=" * 60)

    # Create Raft node
    raft_node = RaftNode(node_id, peers, node_port, wait_for_cluster=wait_for_cluster)
    raft_node.run()  # Initialize node

    # Create RPC handler
    handler = RPCHandler(raft_node)

    # Start RPC server in background thread
    print(f"[Node {node_id}] Starting RPC server on 0.0.0.0:{node_port}")
    rpc_thread = threading.Thread(
        target=rpc_server,
        args=(handler, ('0.0.0.0', node_port), b'raft_cluster', node_ip),
        daemon=True
    )
    rpc_thread.start()

    # Small delay to ensure RPC server is listening
    time.sleep(0.5)

    # Now wait for cluster and start election timers
    raft_node.start_cluster_sync()

    # Keep main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"\n[Server {node_id}] Shutting down...")
        raft_node.stop()
