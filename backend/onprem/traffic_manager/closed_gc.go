package main

import (
	"context"
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// Only already-closed reservations past the collection deadline are garbage.
// Recovery cancel can recreate its exact tombstone; expired raw is never valid.
func (m *manager) gcClosed(root string) int {
	raw, err := read("/proc/uptime", 128)
	if err != nil {
		return 0
	}
	fields := strings.Fields(string(raw))
	if len(fields) != 2 {
		return 0
	}
	now, err := strconv.ParseFloat(fields[0], 64)
	if err != nil || math.IsNaN(now) || math.IsInf(now, 0) {
		return 0
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		return 0
	}
	removed := 0
	for _, entry := range entries {
		token := strings.TrimPrefix(entry.Name(), "cityverse-detached-")
		if entry.Name() == token || !tokenPattern.MatchString(token) {
			continue
		}
		if m.gcClosedOne(root, token, now) {
			removed++
		}
	}
	return removed
}
func (m *manager) gcClosedOne(root, token string, now float64) bool {
	if !tokenPattern.MatchString(token) || !filepath.IsAbs(root) || math.IsNaN(now) || math.IsInf(now, 0) {
		return false
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer cancel()
	unlock, err := m.acquireToken(ctx, token)
	if err != nil {
		return false
	}
	defer unlock()
	dir := filepath.Join(root, "cityverse-detached-"+token)
	info, err := os.Lstat(dir)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return false
	}
	raw, err := read(filepath.Join(dir, "reservation.json"), 65536)
	if err != nil {
		return false
	}
	var r reservation
	var metadata map[string]json.RawMessage
	var deadline float64
	if json.Unmarshal(raw, &r) != nil || json.Unmarshal(raw, &metadata) != nil || json.Unmarshal(metadata["transportDeadline"], &deadline) != nil {
		return false
	}
	if r.Token != token || r.SelectedUID != m.uid || (r.SelectedRole != "client" && r.SelectedRole != "server") || (r.Schema != "cityverse.detached-shard/v1" && r.Schema != "cityverse.detached-shard/v2") || len(r.SelectedBoot) != 36 || r.SelectedNetns == "" || deadline <= 0 || math.IsNaN(deadline) || math.IsInf(deadline, 0) {
		return false
	}
	selected := 0
	for _, role := range r.Roles {
		if role.UID == m.uid && role.Role == r.SelectedRole {
			if role.Pod != m.pod || role.Namespace != m.namespace || role.Boot != r.SelectedBoot || role.Netns != r.SelectedNetns {
				return false
			}
			selected++
		}
	}
	if selected != 1 {
		return false
	}
	// A different kernel boot cannot admit this old reservation. Same-boot
	// records need our exact network namespace and a conservative 60s grace.
	if r.SelectedBoot == m.boot && (r.SelectedNetns != m.netns || now <= deadline+60) {
		return false
	}
	sha := digest(raw)
	claim, err := read(filepath.Join(dir, "claim"), 256)
	if err != nil || string(claim) != sha+" "+r.SelectedBoot+" "+r.SelectedNetns+"\n" {
		return false
	}
	closed, err := read(filepath.Join(dir, "closure"), 128)
	if err != nil || string(closed) != digest([]byte(sha+" cancelled\n"))+"\n" {
		return false
	}
	marker, err := read(filepath.Join(dir, "cancelled"), 32)
	if err != nil || string(marker) != "cancelled" {
		return false
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return false
	}
	for _, entry := range entries {
		file, err := os.Lstat(filepath.Join(dir, entry.Name()))
		if err != nil || !file.Mode().IsRegular() {
			return false
		}
	}
	return os.RemoveAll(dir) == nil
}
