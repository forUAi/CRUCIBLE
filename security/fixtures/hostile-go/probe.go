// Build-phase probes for Go.
//
// Go's distinctive surfaces are different from Node's: no package lifecycle
// hook to abuse, but `go generate` runs arbitrary commands during a build,
// goroutines make thread pressure trivially cheap, and syscall is in the
// standard library rather than behind a native addon.
package main

import (
	"encoding/json"
	"fmt"
	"net"
	"os"
	"os/exec"
	"runtime"
	"strings"
	"sync"
	"syscall"
	"time"
)

var report = map[string]interface{}{}

func probe(name string, fn func() interface{}) {
	defer func() {
		if r := recover(); r != nil {
			report[name] = fmt.Sprintf("BLOCKED: panic %v", r)
		}
	}()
	report[name] = fn()
}

func main() {
	probe("build_phase_exec", func() interface{} {
		// `go generate` and cgo both reach exec during a build.
		out, err := exec.Command("id", "-u").Output()
		if err != nil {
			return "blocked: " + err.Error()
		}
		return "spawned /usr/bin/id, uid " + strings.TrimSpace(string(out))
	})

	probe("host_mount_visible", func() interface{} {
		var hits []string
		for _, p := range []string{"/Users", "/host", "/mnt/host"} {
			if _, err := os.Stat(p); err == nil {
				hits = append(hits, p)
			}
		}
		if len(hits) == 0 {
			return "none"
		}
		return hits
	})

	probe("host_mount_write", func() interface{} {
		homes, err := os.ReadDir("/Users")
		if err != nil {
			return "no host mount reachable"
		}
		for _, h := range homes {
			p := "/Users/" + h.Name() + "/Projects/crucible/CANARY_WRITTEN_FROM_SANDBOX.txt"
			if os.WriteFile(p, []byte("written by go build\n"), 0o644) == nil {
				return "WROTE " + p
			}
		}
		return "no host mount reachable"
	})

	probe("escape_workspace_write", func() interface{} {
		var out []string
		for _, p := range []string{"/etc/crucible-go-canary", "/root/crucible-go-canary"} {
			if err := os.WriteFile(p, []byte("x"), 0o644); err != nil {
				out = append(out, "blocked "+p+": "+err.Error())
			} else {
				out = append(out, "WROTE "+p)
			}
		}
		return out
	})

	probe("symlink_traversal", func() interface{} {
		_ = os.Remove("etc-link")
		if err := os.Symlink("/etc", "etc-link"); err != nil {
			return "blocked: " + err.Error()
		}
		if err := os.WriteFile("etc-link/crucible-go-traversal", []byte("x"), 0o644); err != nil {
			return "blocked: " + err.Error()
		}
		return "WROTE via symlink into /etc"
	})

	probe("credentials", func() interface{} {
		var envs []string
		for _, e := range os.Environ() {
			k := strings.SplitN(e, "=", 2)[0]
			for _, t := range []string{"TOKEN", "SECRET", "_KEY", "AWS", "GITHUB", "GOPRIVATE"} {
				if strings.Contains(strings.ToUpper(k), t) {
					envs = append(envs, k)
					break
				}
			}
		}
		var paths []string
		for _, p := range []string{"/root/.ssh", "/root/.netrc", "/root/.aws",
			"/var/run/docker.sock"} {
			if _, err := os.Stat(p); err == nil {
				paths = append(paths, p)
			}
		}
		return map[string]interface{}{"env": envs, "paths": paths}
	})

	probe("outbound_hardcoded_ip", func() interface{} {
		c, err := net.DialTimeout("tcp", "1.1.1.1:443", 4*time.Second)
		if err != nil {
			return "blocked: " + err.Error()
		}
		c.Close()
		return "REACHED 1.1.1.1:443 without DNS"
	})

	probe("cloud_metadata", func() interface{} {
		c, err := net.DialTimeout("tcp", "169.254.169.254:80", 3*time.Second)
		if err != nil {
			return "blocked: " + err.Error()
		}
		c.Close()
		return "REACHED cloud metadata endpoint"
	})

	probe("goroutine_pressure", func() interface{} {
		// Cheap in Go in a way it is not in most languages: 20k goroutines is
		// a few lines and a few MB.
		var wg sync.WaitGroup
		const n = 20000
		for i := 0; i < n; i++ {
			wg.Add(1)
			go func() { defer wg.Done(); time.Sleep(150 * time.Millisecond) }()
		}
		wg.Wait()
		return fmt.Sprintf("ran %d goroutines on %d OS threads", n, runtime.GOMAXPROCS(0))
	})

	probe("raw_syscall", func() interface{} {
		// syscall is stdlib here, not a native addon behind a build step.
		if err := syscall.Mount("tmpfs", "/mnt", "tmpfs", 0, ""); err != nil {
			return "mount blocked: " + err.Error()
		}
		return "MOUNTED tmpfs at /mnt via raw syscall"
	})

	probe("pid_namespace", func() interface{} {
		ents, _ := os.ReadDir("/proc")
		n := 0
		for _, e := range ents {
			if e.IsDir() && e.Name()[0] >= '0' && e.Name()[0] <= '9' {
				n++
			}
		}
		return fmt.Sprintf("pid=%d visible_pids=%d", os.Getpid(), n)
	})

	b, _ := json.Marshal(report)
	fmt.Println("PROBE_REPORT " + string(b))

	// Reported first; this one is meant to outlive its parent.
	cmd := exec.Command("sh", "-c", "sleep 900")
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	msg := "spawned detached sleep 900"
	if err := cmd.Start(); err != nil {
		msg = "blocked: " + err.Error()
	}
	b2, _ := json.Marshal(map[string]string{"background_daemon": msg})
	fmt.Println("PROBE_REPORT2 " + string(b2))
}
