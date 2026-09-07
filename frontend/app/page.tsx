"use client";

import { useCallback, useEffect, useState } from "react";

type Health = {
  status: string;
  version: string;
};

async function fetchHealth(apiUrl: string): Promise<Health> {
  const response = await fetch(`${apiUrl}/health`, { cache: "no-store" });
  if (!response.ok) throw new Error("Gateway unavailable");
  return (await response.json()) as Health;
}

const providers = [
  { name: "OpenAI", model: "gpt-4o-mini", score: 82, accent: "blue" },
  { name: "Gemini", model: "gemini-2.0-flash", score: 89, accent: "violet" },
  { name: "DeepSeek", model: "deepseek-chat", score: 88, accent: "cyan" },
];

export default function Dashboard() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState(false);
  const apiUrl = process.env.NEXT_PUBLIC_MODELPILOT_API_URL ?? "http://localhost:8000";

  const checkHealth = useCallback(async () => {
    try {
      setHealth(await fetchHealth(apiUrl));
      setError(false);
    } catch {
      setHealth(null);
      setError(true);
    }
  }, [apiUrl]);

  useEffect(() => {
    let active = true;
    void fetchHealth(apiUrl)
      .then((result) => {
        if (!active) return;
        setHealth(result);
        setError(false);
      })
      .catch(() => {
        if (!active) return;
        setHealth(null);
        setError(true);
      });
    return () => {
      active = false;
    };
  }, [apiUrl]);

  return (
    <main>
      <header className="topbar">
        <div className="brand">
          <span className="brandMark" aria-hidden="true">M</span>
          <span>ModelPilot</span>
          <span className="version">V0.1</span>
        </div>
        <div className={`health ${error ? "offline" : ""}`}>
          <span className="healthDot" aria-hidden="true" />
          {health ? `Gateway online · ${health.version}` : error ? "Gateway offline" : "Checking gateway"}
        </div>
      </header>

      <section className="intro">
        <div>
          <p className="eyebrow">ROUTING CONTROL</p>
          <h1>Every request.<br /><span>The right model.</span></h1>
        </div>
        <p className="introCopy">
          A focused view of automatic model selection, provider readiness, and the scoring signals behind every route.
        </p>
      </section>

      <section className="metrics" aria-label="Gateway metrics">
        <article>
          <span>Configured route</span>
          <strong>auto</strong>
          <small>Balanced preference profile</small>
        </article>
        <article>
          <span>Provider adapters</span>
          <strong>03</strong>
          <small>OpenAI · Gemini · DeepSeek</small>
        </article>
        <article>
          <span>Fallback policy</span>
          <strong>ON</strong>
          <small>Highest score attempted first</small>
        </article>
      </section>

      <section className="workspace">
        <div className="panel rankingPanel">
          <div className="panelHeading">
            <div>
              <p className="eyebrow">BASELINE RANKING</p>
              <h2>Provider scorecard</h2>
            </div>
            <span className="formula">Q + C + L + R</span>
          </div>
          <div className="providerList">
            {providers.map((provider, index) => (
              <div className="provider" key={provider.name}>
                <span className="rank">0{index + 1}</span>
                <div className={`providerIcon ${provider.accent}`}>{provider.name[0]}</div>
                <div className="providerIdentity">
                  <strong>{provider.name}</strong>
                  <span>{provider.model}</span>
                </div>
                <div className="scoreTrack" aria-label={`${provider.name} score ${provider.score}`}>
                  <span style={{ width: `${provider.score}%` }} />
                </div>
                <strong className="score">{provider.score}</strong>
              </div>
            ))}
          </div>
        </div>

        <aside className="panel signalPanel">
          <p className="eyebrow">ROUTING SIGNALS</p>
          <h2>Balanced by default</h2>
          <div className="signals">
            {[
              ["Quality", "25%"],
              ["Cost", "25%"],
              ["Latency", "25%"],
              ["Reliability", "25%"],
            ].map(([label, value]) => (
              <div key={label}>
                <span>{label}</span>
                <strong>{value}</strong>
              </div>
            ))}
          </div>
          <button type="button" onClick={() => void checkHealth()}>Refresh gateway status</button>
        </aside>
      </section>

      <footer>
        <span>MODEL PILOT / LOCAL CONTROL PLANE</span>
        <span>{apiUrl}</span>
      </footer>
    </main>
  );
}
