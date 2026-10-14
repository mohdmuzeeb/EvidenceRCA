#!/bin/sh
# Read-only host watcher during capture: power source, disk, memory. Never touches the workload.
while true; do
  P=$(pmset -g batt | head -1 | sed "s/Now drawing from //")
  D=$(df -g / | tail -1 | awk '{print $4}')
  M=$(sysctl -n kern.memorystatus_level)
  echo "$(date +%FT%T) power=$P disk_free_gb=$D mem_free_pct=$M"
  sleep 60
done
