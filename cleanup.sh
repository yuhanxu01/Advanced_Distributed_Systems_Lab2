#!/bin/bash
# Cleanup script for Raft Lab 2
# Removes all persistent state files to start fresh

echo "=================================================="
echo "Raft Lab 2 - Cleanup Script"
echo "=================================================="
echo ""
echo "This script will remove all persistent state files:"
echo "  - raft_state_node*.json (Raft persistent state)"
echo "  - CISC6935_node*.log (State machine log files)"
echo ""
echo "⚠ WARNING: This will delete all uncommitted data!"
echo ""
read -p "Continue? (y/n): " -n 1 -r
echo

if [[ $REPLY =~ ^[Yy]$ ]]
then
    echo ""
    echo "Removing state files..."

    # Remove Raft state files
    rm -f raft_state_node*.json
    echo "  ✓ Removed raft_state_node*.json"

    # Remove log files
    rm -f CISC6935_node*.log
    echo "  ✓ Removed CISC6935_node*.log"

    echo ""
    echo "✓ Cleanup completed!"
    echo ""
    echo "Next steps:"
    echo "  1. Restart all Raft nodes:"
    echo "     python3 server.py 0"
    echo "     python3 server.py 1"
    echo "     python3 server.py 2"
    echo "  2. Run tests from client:"
    echo "     python3 client.py"
    echo ""
else
    echo ""
    echo "Cleanup cancelled."
fi
