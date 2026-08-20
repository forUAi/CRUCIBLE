// Goroutines are not OS processes. This measures which control actually
// bounds them: pids.max counts tasks, and 20000 goroutines multiplex onto
// GOMAXPROCS OS threads, so the pid ceiling never sees them. Memory does.
package main

import (
	"encoding/json"
	"fmt"
	"runtime"
	"sync"
	"time"
)

func main() {
	var wg sync.WaitGroup
	const n = 200000
	started := 0
	var m runtime.MemStats
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); time.Sleep(300 * time.Millisecond) }()
		started++
	}
	runtime.ReadMemStats(&m)
	threads := runtime.GOMAXPROCS(0)
	wg.Wait()
	b, _ := json.Marshal(map[string]interface{}{
		"goroutines_started": started,
		"os_threads":         threads,
		"heap_mb":            m.HeapAlloc / 1024 / 1024,
		"note": "goroutines are multiplexed onto OS threads; a pid cgroup " +
			"counts tasks, so it never sees them",
	})
	fmt.Println("GOROUTINE_REPORT " + string(b))
}
