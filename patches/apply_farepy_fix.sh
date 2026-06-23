#!/bin/bash
# One-command application of the Farepy round-trip fix

set -e

cd "$(dirname "$0")/.."

echo "Applying Farepy integration..."

# Apply patches
git apply patches/farepy_integration.patch 2>/dev/null || echo "Import patch may already be applied"
git apply patches/roundtrip_logic.patch 2>/dev/null || echo "Logic patch may already be applied"

echo "Done. You can now test with:"
echo "  python scripts/test_roundtrip.py"
echo "  python -m flight_deals search --category italian-gems --return 3-7"