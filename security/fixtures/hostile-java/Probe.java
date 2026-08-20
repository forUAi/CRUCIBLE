// Build-phase probes for the JVM.
//
// The JVM's distinctive surfaces: the build tool runs arbitrary plugin code
// (this file is compiled and executed *during the build*, which is exactly
// what a Maven or Gradle plugin does), Runtime.exec is in the standard
// library, threads are OS threads, and the security manager that used to
// bound any of this was removed in JDK 18.
import java.io.File;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public class Probe {
    static final Map<String, Object> R = new LinkedHashMap<>();

    interface P { Object run() throws Exception; }

    static void probe(String name, P p) {
        try { R.put(name, p.run()); }
        catch (Throwable t) {
            R.put(name, "BLOCKED: " + t.getClass().getSimpleName() + ": "
                        + String.valueOf(t.getMessage()).substring(
                            0, Math.min(70, String.valueOf(t.getMessage()).length())));
        }
    }

    public static void main(String[] args) throws Exception {
        probe("build_plugin_executed", () ->
            "compiled and run during the build as uid "
            + exec("id", "-u").trim() + ", jdk " + System.getProperty("java.version"));

        probe("host_mount_visible", () -> {
            List<String> hits = new ArrayList<>();
            for (String p : new String[]{"/Users", "/host", "/mnt/host"})
                if (new File(p).exists()) hits.add(p);
            return hits.isEmpty() ? "none" : hits;
        });

        probe("host_mount_write", () -> {
            File users = new File("/Users");
            File[] homes = users.listFiles();
            if (homes == null) return "no host mount reachable";
            for (File h : homes) {
                Path p = Path.of(h.getPath(), "Projects", "crucible",
                                 "CANARY_WRITTEN_FROM_SANDBOX.txt");
                try {
                    Files.writeString(p, "written by a jvm build plugin\n");
                    return "WROTE " + p;
                } catch (IOException e) { /* next home */ }
            }
            return "no host mount reachable";
        });

        probe("escape_workspace_write", () -> {
            List<String> out = new ArrayList<>();
            for (String p : new String[]{"/etc/crucible-java-canary",
                                         "/root/crucible-java-canary"}) {
                try { Files.writeString(Path.of(p), "x"); out.add("WROTE " + p); }
                catch (IOException e) { out.add("blocked " + p + ": " + e.getMessage()); }
            }
            return out;
        });

        probe("process_execution", () ->
            "Runtime.exec reachable: " + exec("sh", "-c", "echo ok").trim());

        probe("credentials", () -> {
            List<String> env = new ArrayList<>();
            for (String k : System.getenv().keySet()) {
                String u = k.toUpperCase();
                if (u.contains("TOKEN") || u.contains("SECRET") || u.contains("_KEY")
                    || u.contains("AWS") || u.contains("GITHUB") || u.contains("MAVEN"))
                    env.add(k);
            }
            List<String> paths = new ArrayList<>();
            for (String p : new String[]{"/root/.ssh", "/root/.m2/settings.xml",
                                         "/root/.gradle", "/var/run/docker.sock"})
                if (new File(p).exists()) paths.add(p);
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("env", env);
            m.put("paths", paths);
            return m;
        });

        probe("outbound_hardcoded_ip", () -> {
            try (Socket s = new Socket()) {
                s.connect(new InetSocketAddress("1.1.1.1", 443), 4000);
                return "REACHED 1.1.1.1:443 without DNS";
            } catch (IOException e) {
                return "blocked: " + e.getClass().getSimpleName();
            }
        });

        probe("thread_pressure", () -> {
            // JVM threads are OS threads; this is real pressure, not green
            // threads. Bounded so it stresses the cap without wedging a host.
            List<Thread> ts = new ArrayList<>();
            int made = 0;
            try {
                for (int i = 0; i < 2000; i++) {
                    Thread t = new Thread(() -> {
                        try { Thread.sleep(200); } catch (InterruptedException ignored) {}
                    });
                    t.start(); ts.add(t); made++;
                }
            } catch (Throwable t) {
                return "capped after " + made + " threads: " + t.getClass().getSimpleName();
            }
            for (Thread t : ts) t.join();
            return "started " + made + " OS threads with no cap hit";
        });

        probe("security_manager_gone", () ->
            System.getSecurityManager() == null
                ? "no SecurityManager (removed in JDK 18); nothing in-process bounds this"
                : "SecurityManager present");

        probe("native_library_surface", () -> {
            try { System.loadLibrary("definitely-not-a-real-library"); return "LOADED"; }
            catch (Throwable t) { return "loadLibrary reachable, lookup failed: "
                                         + t.getClass().getSimpleName(); }
        });

        System.out.println("PROBE_REPORT " + json(R));

        // Reported first; this one is meant to outlive the JVM.
        Map<String, Object> after = new LinkedHashMap<>();
        try {
            new ProcessBuilder("sh", "-c", "setsid sleep 900 </dev/null >/dev/null 2>&1 &")
                .start().waitFor();
            after.put("background_daemon", "spawned detached sleep 900");
        } catch (Exception e) {
            after.put("background_daemon", "blocked: " + e.getMessage());
        }
        System.out.println("PROBE_REPORT2 " + json(after));
    }

    static String exec(String... cmd) throws IOException, InterruptedException {
        Process p = new ProcessBuilder(cmd).redirectErrorStream(true).start();
        String out = new String(p.getInputStream().readAllBytes());
        p.waitFor();
        return out;
    }

    /** Minimal JSON so the fixture needs no dependency to be fetched. */
    static String json(Object o) {
        if (o instanceof Map<?, ?> m) {
            StringBuilder sb = new StringBuilder("{");
            boolean first = true;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (!first) sb.append(",");
                first = false;
                sb.append(quote(String.valueOf(e.getKey()))).append(":").append(json(e.getValue()));
            }
            return sb.append("}").toString();
        }
        if (o instanceof List<?> l) {
            StringBuilder sb = new StringBuilder("[");
            for (int i = 0; i < l.size(); i++) {
                if (i > 0) sb.append(",");
                sb.append(json(l.get(i)));
            }
            return sb.append("]").toString();
        }
        return quote(String.valueOf(o));
    }

    static String quote(String s) {
        StringBuilder sb = new StringBuilder("\"");
        for (char c : s.toCharArray()) {
            switch (c) {
                case '"' -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                default -> { if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
                             else sb.append(c); }
            }
        }
        return sb.append("\"").toString();
    }
}
