// Replay buttons + spoken-order proposals for the TP order-entry screen.
//
// <AudioAgentPanel onLoadCipher={(cipher, proposal) => setKoeInput(cipher)} />
//
// SAFETY: "Load" only puts the draft cipher into the KOE input, exactly as if the trader
// had typed it. It does not press Enter, price, log, or send anything; the backend KOE
// parser validates it as usual. Quantity from speech is the BROKER's size (Qty), never
// My Qty, which the trader always enters.
import { useEffect } from "react";
import { useAudioAgent } from "./useAudioAgent";

const C = {
  bg: "#11151c", panel: "#171c25", line: "#2a3140", text: "#d7dde8", dim: "#8a94a6",
  ok: "#3fb950", warn: "#d29922", bad: "#f85149", accent: "#58a6ff",
};

const FIELD_LABELS = {
  ticker: "Ticker", expiries: "Exp", strikes: "Strikes", cp: "C/P", spread_type: "Type",
  ratio: "Ratio", broker_side: "Broker side", broker_qty: "Broker Qty", price: "Price",
};
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function fmt(name, v) {
  if (v === null || v === undefined) return "—";
  if (name === "expiries") return v.map((e) => `${MONTHS[e.month - 1]}${e.day ? " " + e.day : ""}${e.year ? " " + e.year : ""}`).join(" / ");
  if (name === "broker_side") return { b: "B (broker buys)", s: "S (broker sells)", two_sided: "2-sided" }[v] || v;
  if (Array.isArray(v)) return v.join(name === "ratio" ? "x" : " / ");
  return String(v);
}

function Lamp({ status }) {
  const color = status === "ok" ? C.ok : status === "degraded" ? C.warn : C.bad;
  return <span title={`audio agent: ${status}`} style={{ display: "inline-block", width: 10, height: 10, borderRadius: 5, background: color, marginRight: 8 }} />;
}

function Btn({ children, onClick, disabled, title, tone }) {
  return (
    <button onClick={onClick} disabled={disabled} title={title}
      style={{ background: tone === "primary" ? C.accent : "transparent", color: tone === "primary" ? "#0b0f14" : C.text,
               border: `1px solid ${tone === "primary" ? C.accent : C.line}`, borderRadius: 4, padding: "4px 10px",
               fontSize: 13, cursor: disabled ? "default" : "pointer", opacity: disabled ? 0.4 : 1, marginRight: 6 }}>
      {children}
    </button>
  );
}

function ProposalCard({ p, onLoad, onDismiss }) {
  const age = Math.max(0, Math.round(Date.now() / 1000 - p.created_ts));
  return (
    <div style={{ border: `1px solid ${C.line}`, borderRadius: 6, padding: 10, marginTop: 8, background: C.bg }}>
      <div style={{ color: C.dim, fontSize: 12, marginBottom: 6 }}>
        heard {age}s ago · ASR {Math.round(p.asr_conf * 100)}% · {p.extractor}
      </div>
      {p.transcript && <div style={{ fontStyle: "italic", marginBottom: 8 }}>“{p.transcript}”</div>}
      <table style={{ fontSize: 13, borderCollapse: "collapse", marginBottom: 8 }}>
        <tbody>
          {Object.entries(FIELD_LABELS).map(([k, label]) => {
            const f = p.fields[k];
            const blank = f.value === null || f.value === undefined;
            const required = p.missing.includes(k);
            return (
              <tr key={k}>
                <td style={{ color: C.dim, paddingRight: 12 }}>{label}</td>
                <td style={{ color: blank ? (required ? C.warn : C.dim) : C.text, fontWeight: blank ? 400 : 500 }}>
                  {fmt(k, f.value)}{blank && required ? "  (needed)" : ""}
                </td>
                <td style={{ color: C.dim, paddingLeft: 12, fontSize: 11 }}>{f.conf ? `${Math.round(f.conf * 100)}%` : ""}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div style={{ fontFamily: "ui-monospace, Consolas, monospace", fontSize: 14, marginBottom: 8, color: p.cipher ? C.text : C.warn }}>
        {p.cipher || `no cipher — missing: ${p.missing.join(", ")}`}
      </div>
      {p.notes?.length > 0 && <div style={{ color: C.dim, fontSize: 12, marginBottom: 8 }}>{p.notes.join(" · ")}</div>}
      <Btn tone="primary" disabled={!p.cipher} onClick={() => onLoad(p)} title="Put the draft into the KOE input. Nothing is priced or sent.">Load into KOE</Btn>
      <Btn onClick={() => onDismiss(p)}>Dismiss</Btn>
    </div>
  );
}

export default function AudioAgentPanel({ onLoadCipher, url, token, hotkeys = true }) {
  const a = useAudioAgent({ url, token });
  const opts = a.health?.replay?.options || [30, 60];

  useEffect(() => {
    if (!hotkeys) return;
    // F9 = replay default, Shift+F9 = last order, Esc while replaying = stop
    const onKey = (e) => {
      if (e.key === "F9" && !e.shiftKey) { e.preventDefault(); a.replay(); }
      else if (e.key === "F9" && e.shiftKey) { e.preventDefault(); a.replayLastOrder(); }
      else if (e.key === "Escape" && a.replaying) { a.stopReplay(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [hotkeys, a]);

  return (
    <div style={{ background: C.panel, color: C.text, border: `1px solid ${C.line}`, borderRadius: 8, padding: 12, fontSize: 13 }}>
      <div style={{ display: "flex", alignItems: "center", marginBottom: 8 }}>
        <Lamp status={a.status} />
        <span style={{ fontWeight: 500, marginRight: 16 }}>Audio</span>
        {opts.map((s) => (
          <Btn key={s} disabled={a.status === "offline"} onClick={() => a.replay(s)} title={`Replay the last ${s} seconds`}>◀ {s}s</Btn>
        ))}
        <Btn disabled={a.status === "offline"} onClick={a.replayLastOrder} title="Replay the broker's last utterance">◀ Last order</Btn>
        {a.replaying && <Btn onClick={a.stopReplay}>■ Stop</Btn>}
      </div>
      {a.status === "offline" && <div style={{ color: C.dim }}>Audio agent not running on this PC.</div>}
      {a.lastError && <div style={{ color: C.warn }}>{a.lastError}</div>}
      {a.proposals.length > 0 && (
        <div style={{ color: C.dim, fontSize: 12 }}>Spoken orders — drafts only. Broker Qty is the broker's size; enter My Qty yourself.</div>
      )}
      {a.proposals.map((p) => (
        <ProposalCard key={p.id} p={p}
          onLoad={(pp) => { onLoadCipher?.(pp.cipher, pp); a.ack(pp.id, "loaded"); }}
          onDismiss={(pp) => a.ack(pp.id, "dismissed")} />
      ))}
    </div>
  );
}
