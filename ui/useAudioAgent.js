// React hook for the desk-local TP Audio Agent. Drop into trading-ui (src/hooks/).
//
// The agent runs on the trader's own PC at http://127.0.0.1:8765 and must list the TP
// origin in agent.allowed_origins. If it isn't running, `status` is "offline" and every
// action is a no-op — TP keeps working exactly as before.
import { useCallback, useEffect, useRef, useState } from "react";

const DEFAULT_URL = "http://127.0.0.1:8765";

export function useAudioAgent({ url = DEFAULT_URL, token = "" } = {}) {
  const [status, setStatus] = useState("offline"); // offline | ok | degraded
  const [health, setHealth] = useState(null);
  const [proposals, setProposals] = useState([]);
  const [replaying, setReplaying] = useState(false);
  const [lastError, setLastError] = useState(null);
  const esRef = useRef(null);

  useEffect(() => {
    const q = token ? `?token=${encodeURIComponent(token)}` : "";
    const es = new EventSource(`${url}/events${q}`);
    esRef.current = es;
    const parse = (e) => JSON.parse(e.data);
    const onHealth = (h) => {
      setHealth(h);
      setStatus(h.status === "ok" ? "ok" : "degraded");
      setReplaying(!!h.replay?.playing);
    };
    es.addEventListener("hello", (e) => {
      const d = parse(e);
      setProposals(d.proposals || []);
      onHealth(d.health);
    });
    es.addEventListener("health", (e) => onHealth(parse(e)));
    es.addEventListener("proposal.new", (e) => {
      const p = parse(e);
      setProposals((ps) => [p, ...ps.filter((x) => x.id !== p.id)]);
    });
    es.addEventListener("proposal.closed", (e) => {
      const { id } = parse(e);
      setProposals((ps) => ps.filter((x) => x.id !== id));
    });
    es.addEventListener("replay.started", () => setReplaying(true));
    es.addEventListener("replay.stopped", () => setReplaying(false));
    es.onerror = () => setStatus("offline"); // EventSource retries on its own
    return () => es.close();
  }, [url, token]);

  const post = useCallback(
    async (path, body) => {
      try {
        const r = await fetch(`${url}${path}`, {
          method: "POST",
          headers: { "Content-Type": "application/json", ...(token ? { "X-Agent-Token": token } : {}) },
          body: JSON.stringify(body || {}),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
        setLastError(null);
        return j;
      } catch (err) {
        setLastError(String(err.message || err));
        return null;
      }
    },
    [url, token]
  );

  const replay = useCallback(
    (seconds, roles) => post("/replay", { seconds, ...(roles ? { roles } : {}) }),
    [post]
  );
  const replayLastOrder = useCallback(() => post("/replay", { target: "last_segment" }), [post]);
  const stopReplay = useCallback(() => post("/replay/stop"), [post]);
  const ack = useCallback(
    (id, action) => {
      setProposals((ps) => ps.filter((x) => x.id !== id));
      return post(`/proposals/${id}/ack`, { action });
    },
    [post]
  );

  return { status, health, proposals, replaying, lastError, replay, replayLastOrder, stopReplay, ack };
}
