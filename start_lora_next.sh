#!/usr/bin/env bash
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/autodl/start_lora_next.sh" "$@"
