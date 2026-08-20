// Runtime phase: the pod netns has no route out. Serve so the oracle verifies.
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.Socket;

public class Server {
    public static void main(String[] args) throws IOException {
        String result;
        try (Socket s = new Socket()) {
            s.connect(new InetSocketAddress("1.1.1.1", 443), 3000);
            result = "REACHED 1.1.1.1:443 at runtime";
        } catch (IOException e) {
            result = "blocked at runtime: " + e.getClass().getSimpleName();
        }
        System.out.println("RUNTIME_PROBE {\"runtime_egress\": \"" + result + "\"}");
        final String body = result;
        HttpServer h = HttpServer.create(new InetSocketAddress("0.0.0.0", 8000), 0);
        h.createContext("/", ex -> {
            ex.sendResponseHeaders(200, body.length());
            ex.getResponseBody().write(body.getBytes());
            ex.getResponseBody().close();
        });
        h.start();
    }
}
