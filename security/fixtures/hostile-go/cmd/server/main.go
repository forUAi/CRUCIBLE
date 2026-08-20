// Runtime phase: the pod netns has no route out. Serve so the oracle verifies.
package main

import (
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"time"
)

func main() {
	result := "blocked at runtime"
	if c, err := net.DialTimeout("tcp", "1.1.1.1:443", 3*time.Second); err == nil {
		c.Close()
		result = "REACHED 1.1.1.1:443 at runtime"
	} else {
		result = "blocked at runtime: " + err.Error()
	}
	b, _ := json.Marshal(map[string]string{"runtime_egress": result})
	fmt.Println("RUNTIME_PROBE " + string(b))
	http.HandleFunc("/", func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprint(w, result)
	})
	http.ListenAndServe("0.0.0.0:8000", nil)
}
