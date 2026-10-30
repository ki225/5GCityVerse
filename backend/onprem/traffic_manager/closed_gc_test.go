package main

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func gcFixture(t *testing.T, m *manager, root, token string, mutate func(map[string]any)) string {
	t.Helper()
	v := map[string]any{"token": token, "schemaVersion": "cityverse.detached-shard/v2", "selectedRole": "server", "selectedPodUid": m.uid, "selectedBootId": m.boot, "selectedNetns": m.netns, "transportDeadline": 1000.0,
		"roles": []role{{Role: "server", UID: m.uid, Pod: m.pod, Namespace: m.namespace, Boot: m.boot, Netns: m.netns}}}
	if mutate != nil {
		mutate(v)
	}
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	dir := filepath.Join(root, "cityverse-detached-"+token)
	if err = os.Mkdir(dir, 0700); err != nil {
		t.Fatal(err)
	}
	sha := digest(raw)
	boot := v["selectedBootId"].(string)
	netns := v["selectedNetns"].(string)
	for name, value := range map[string]string{"reservation.json": string(raw), "claim": sha + " " + boot + " " + netns + "\n", "closure": digest([]byte(sha+" cancelled\n")) + "\n", "cancelled": "cancelled", "raw": strings.Repeat("x", 200000)} {
		if err = os.WriteFile(filepath.Join(dir, name), []byte(value), 0600); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}
func TestClosedGCRejectsUncertainAndActiveState(t *testing.T) {
	for _, fault := range []string{"none", "not_expired", "foreign_uid", "foreign_role", "claim", "closure", "marker", "missing_closure", "symlink", "subdir", "same_boot_other_netns", "invalid_deadline", "locked"} {
		t.Run(fault, func(t *testing.T) {
			m := testManager(t)
			root := t.TempDir()
			token := strings.Repeat("c", 32)
			mutate := func(v map[string]any) {
				if fault == "foreign_uid" {
					v["selectedPodUid"] = "foreign"
				}
				if fault == "foreign_role" {
					v["roles"] = []role{}
				}
				if fault == "same_boot_other_netns" {
					v["selectedNetns"] = "net:[999999]"
					v["roles"] = []role{{Role: "server", UID: m.uid, Pod: m.pod, Namespace: m.namespace, Boot: m.boot, Netns: "net:[999999]"}}
				}
				if fault == "invalid_deadline" {
					v["transportDeadline"] = -1
				}
			}
			dir := gcFixture(t, m, root, token, mutate)
			if fault == "claim" || fault == "closure" || fault == "marker" {
				name := fault
				if name == "marker" {
					name = "cancelled"
				}
				os.WriteFile(filepath.Join(dir, name), []byte("changed"), 0600)
			}
			if fault == "missing_closure" {
				os.Remove(filepath.Join(dir, "closure"))
			}
			if fault == "symlink" {
				os.Symlink("/etc/passwd", filepath.Join(dir, "external"))
			}
			if fault == "subdir" {
				os.Mkdir(filepath.Join(dir, "external"), 0700)
			}
			now := 2000.0
			if fault == "not_expired" {
				now = 1060
			}
			if fault == "locked" {
				unlock, err := m.acquireToken(context.Background(), token)
				if err != nil {
					t.Fatal(err)
				}
				defer unlock()
			}
			removed := m.gcClosedOne(root, token, now)
			if removed != (fault == "none") {
				t.Fatalf("removed=%v", removed)
			}
			_, err := os.Stat(dir)
			if (err == nil) == removed {
				t.Fatalf("directory existence inconsistent: %v", err)
			}
		})
	}
}
func TestClosedGCAcrossPreviousKernelBoot(t *testing.T) {
	m := testManager(t)
	root := t.TempDir()
	token := strings.Repeat("d", 32)
	old := "01234567-89ab-cdef-0123-456789abcdef"
	gcFixture(t, m, root, token, func(v map[string]any) {
		v["selectedBootId"] = old
		v["roles"] = []role{{Role: "server", UID: m.uid, Pod: m.pod, Namespace: m.namespace, Boot: old, Netns: m.netns}}
	})
	if !m.gcClosedOne(root, token, 1) {
		t.Fatal("closed previous-kernel reservation retained")
	}
}
func TestClosedGCDoesNotRemoveSymlinkDirectory(t *testing.T) {
	m := testManager(t)
	root := t.TempDir()
	target := t.TempDir()
	token := strings.Repeat("e", 32)
	os.WriteFile(filepath.Join(target, "keep"), []byte("keep"), 0600)
	os.Symlink(target, filepath.Join(root, "cityverse-detached-"+token))
	if m.gcClosedOne(root, token, 2000) {
		t.Fatal("symlink accepted")
	}
	if _, err := os.Stat(filepath.Join(target, "keep")); err != nil {
		t.Fatal(err)
	}
}
func TestClosedGCReclaimsManyExpiredReservations(t *testing.T) {
	m := testManager(t)
	root := t.TempDir()
	for i := 0; i < 128; i++ {
		token := strings.Repeat("0", 30) + string("0123456789abcdef"[i/16]) + string("0123456789abcdef"[i%16])
		gcFixture(t, m, root, token, nil)
	}
	for i := 0; i < 128; i++ {
		token := strings.Repeat("0", 30) + string("0123456789abcdef"[i/16]) + string("0123456789abcdef"[i%16])
		if !m.gcClosedOne(root, token, 2000) {
			t.Fatal("expired reservation retained")
		}
	}
	if len(m.locks) != 0 {
		t.Fatal("gc leaked token locks")
	}
}
