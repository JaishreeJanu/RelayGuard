import React, { useState, useEffect } from 'react';

export default function App() {
  // --- TELEMETRY STATES ---
  const [backendData, setBackendData] = useState({
    circuit_breaker: { state: 'CLOSED', consecutive_failures: 0 },
    idempotency: { blocked_duplicates: 0 },
    database_metrics: { PENDING: 0, SENT: 0, FAILED: 0, DLQ: 0 },
    recent_notifications: []
  });
  const [vendorState, setVendorState] = useState('HEALTHY');
  
  // --- INTERACTION MUTATION STATES ---
  const [mutatingChaos, setMutatingChaos] = useState(false);
  const [flushingBacklog, setFlushingBacklog] = useState(false);
  const [includeDlq, setIncludeDlq] = useState(false);
  const [reconciliationMessage, setReconciliationMessage] = useState(null);

  // --- ENGINE DATA PIPELINE ---
  const fetchTelemetry = async () => {
    try {
      const backendRes = await fetch('http://localhost:8000/api/v1/notifications/status');
      const backendJson = await backendRes.json();
      setBackendData(backendJson);

      const vendorRes = await fetch('http://localhost:8001/chaos/state');
      const vendorJson = await vendorRes.json();
      setVendorState(vendorJson.current_state);
    } catch (error) {
      console.error("Observability data link error:", error);
    }
  };

  const toggleChaosState = async (targetState) => {
    setMutatingChaos(true);
    try {
      const response = await fetch('http://localhost:8001/chaos/state', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ state: targetState })
      });
      if (response.ok) setVendorState(targetState);
    } catch (error) {
      console.error("Failed to inject system chaos:", error);
    } finally {
      setMutatingChaos(false);
    }
  };

  // =========================================================================
  // 🔄 SUB-STEP 5.5: RECONCILIATION FLUSH OPERATION HANDLER
  // =========================================================================
  const triggerBacklogFlush = async () => {
    setFlushingBacklog(true);
    setReconciliationMessage(null);
    try {
      const response = await fetch(`http://localhost:8000/api/v1/notifications/requeue-backlog?include_dlq=${includeDlq}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
      });
      const json = await response.json();
      
      if (response.ok) {
        setReconciliationMessage({ type: 'success', text: json.message });
        // Immediately fetch telemetry to reflect the changes in the UI
        fetchTelemetry();
      } else {
        setReconciliationMessage({ type: 'error', text: 'Reconciliation sequence failed to execute.' });
      }
    } catch (error) {
      console.error("Backlog pipeline handoff failed:", error);
      setReconciliationMessage({ type: 'error', text: 'Network connection disruption to API gateway.' });
    } finally {
      setFlushingBacklog(false);
      // Auto-clear notification toast banner after 4 seconds
      setTimeout(() => setReconciliationMessage(null), 4000);
    }
  };

  useEffect(() => {
    fetchTelemetry();
    const interval = setInterval(fetchTelemetry, 3000);
    return () => clearInterval(interval);
  }, []);

  const cb = backendData.circuit_breaker;
  const metrics = backendData.database_metrics;
  const notifications = backendData.recent_notifications || [];

  const getStatusBadge = (status) => {
    switch (status) {
      case 'SENT': return 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20';
      case 'PENDING': return 'bg-amber-500/10 text-amber-400 border-amber-500/20 animate-pulse';
      case 'FAILED': return 'bg-red-500/10 text-red-400 border-red-500/20';
      case 'DLQ': return 'bg-purple-500/10 text-purple-400 border-purple-500/20 font-bold';
      default: return 'bg-slate-500/10 text-slate-400 border-slate-500/20';
    }
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 font-sans pb-12">
      {/* NAVBAR */}
      <header className="border-b border-slate-900 bg-slate-900/30 backdrop-blur-md sticky top-0 z-50 px-6 py-4">
        <div className="max-w-7xl mx-auto flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="h-9 w-9 rounded-lg bg-indigo-500/10 text-indigo-400 border border-indigo-500/20 flex items-center justify-center font-bold">🛡️</div>
            <div>
              <h1 className="text-lg font-bold tracking-tight text-white leading-tight">RelayGuard</h1>
              <p className="text-xs text-slate-500 font-medium">Observability Hub & Webhook Gateway</p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-indigo-500/10 text-indigo-400 border border-indigo-500/20 text-xs font-bold shadow-inner">
              🛡️ Duplicates Blocked: {backendData.idempotency?.blocked_duplicates || 0}
            </div>
            <div className="flex items-center gap-2 px-3 py-1 rounded-full bg-slate-900 border border-slate-800 text-xs font-semibold text-slate-400">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-500 animate-pulse"></span>
              Telemetry Stream Live
            </div>
          </div>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-6 mt-8">
        {/* HUD GRID */}
        <h2 className="text-xs font-bold uppercase tracking-widest text-slate-500 mb-4">Core Infrastructure HUD</h2>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
          {/* CIRCUIT BREAKER CARD */}
          <div className="border border-slate-900 bg-slate-900/40 rounded-xl p-6 backdrop-blur-sm shadow-xl flex flex-col justify-between">
            <div>
              <div className="text-xs text-slate-500 font-bold uppercase tracking-wider mb-1">Circuit Breaker Status</div>
              <div className="text-sm font-semibold text-slate-400 mb-4">Redis Namespace Coordinator</div>
            </div>
            <div className="flex items-center justify-between">
              <span className={`inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-bold border ${cb.state === 'CLOSED' ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20' : 'bg-red-500/10 text-red-400 border-red-500/20 animate-pulse'}`}>
                <span className={`h-2 w-2 rounded-full ${cb.state === 'CLOSED' ? 'bg-emerald-400' : 'bg-red-400'}`}></span>
                {cb.state}
              </span>
              <div className="text-right">
                <div className="text-2xl font-black text-white">{cb.consecutive_failures}</div>
                <div className="text-[10px] uppercase font-bold tracking-wider text-slate-500">Consecutive Drops</div>
              </div>
            </div>
          </div>

          {/* DOWNSTREAM VENDOR CARD */}
          <div className="border border-slate-900 bg-slate-900/40 rounded-xl p-6 backdrop-blur-sm shadow-xl flex flex-col justify-between">
            <div>
              <div className="text-xs text-slate-500 font-bold uppercase tracking-wider mb-1">Mock Downstream Vendor</div>
              <div className="text-sm font-semibold text-slate-400 mb-4">Email API Gateway Simulator</div>
            </div>
            <div className="flex items-center justify-between">
              <span className={`inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-bold border ${vendorState === 'HEALTHY' ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20' : vendorState === 'ERROR_500' ? 'bg-red-500/10 text-red-400 border-red-500/20 animate-pulse' : 'bg-amber-500/10 text-amber-400 border-amber-500/20 animate-pulse'}`}>
                <span className={`h-2 w-2 rounded-full ${vendorState === 'HEALTHY' ? 'bg-emerald-400' : vendorState === 'ERROR_500' ? 'bg-red-400' : 'bg-amber-400'}`}></span>
                {vendorState}
              </span>
              <div className="text-xs font-semibold text-slate-500 italic">Targeting Port 8001</div>
            </div>
          </div>

          {/* DATABASE QUANTITIES CARD */}
          <div className="border border-slate-900 bg-slate-900/40 rounded-xl p-6 backdrop-blur-sm shadow-xl">
            <div className="text-xs text-slate-500 font-bold uppercase tracking-wider mb-3">Database State Quantities</div>
            <div className="grid grid-cols-4 gap-2 text-center">
              <div className="bg-slate-950/50 border border-slate-900/80 p-2.5 rounded-lg">
                <div className="text-xs font-bold text-amber-400">{metrics.PENDING}</div>
                <div className="text-[9px] uppercase font-bold tracking-tight text-slate-500 mt-0.5">Pend</div>
              </div>
              <div className="bg-slate-950/50 border border-slate-900/80 p-2.5 rounded-lg">
                <div className="text-xs font-bold text-emerald-400">{metrics.SENT}</div>
                <div className="text-[9px] uppercase font-bold tracking-tight text-slate-500 mt-0.5">Sent</div>
              </div>
              <div className="bg-slate-950/50 border border-slate-900/80 p-2.5 rounded-lg">
                <div className="text-xs font-bold text-red-400">{metrics.FAILED}</div>
                <div className="text-[9px] uppercase font-bold tracking-tight text-slate-500 mt-0.5">Fail</div>
              </div>
              <div className="bg-slate-950/50 border border-slate-900/80 p-2.5 rounded-lg">
                <div className="text-xs font-bold text-purple-400">{metrics.DLQ}</div>
                <div className="text-[9px] uppercase font-bold tracking-tight text-slate-500 mt-0.5">DLQ</div>
              </div>
            </div>
          </div>
        </div>

        {/* CONTROLS HUB ACTION GRID (Chaos + Reconciliation Side-by-Side) */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
          
          {/* CHAOS PANEL */}
          <div className="border border-slate-900 bg-slate-900/20 rounded-xl p-6 backdrop-blur-sm shadow-xl flex flex-col justify-between">
            <div>
              <h3 className="text-sm font-bold text-white mb-1">Chaos Fault Injection Switchboard</h3>
              <p className="text-xs text-slate-400 mb-4">Simulate edge exceptions or vendor crashes dynamically to evaluate circuit boundaries.</p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <button disabled={mutatingChaos} onClick={() => toggleChaosState('HEALTHY')} className={`flex-1 min-w-[100px] px-3 py-2 rounded-lg text-xs font-bold tracking-wide border transition-all duration-200 ${vendorState === 'HEALTHY' ? 'bg-emerald-500 text-white border-emerald-400 shadow-md' : 'bg-slate-900 text-slate-400 border-slate-800 hover:text-slate-200'}`}>💚 HEALTHY</button>
              <button disabled={mutatingChaos} onClick={() => toggleChaosState('ERROR_500')} className={`flex-1 min-w-[100px] px-3 py-2 rounded-lg text-xs font-bold tracking-wide border transition-all duration-200 ${vendorState === 'ERROR_500' ? 'bg-red-500 text-white border-red-400 shadow-md shadow-red-500/20 animate-pulse' : 'bg-slate-900 text-slate-400 border-slate-800 hover:text-slate-200'}`}>💥 CRASH (500)</button>
              <button disabled={mutatingChaos} onClick={() => toggleChaosState('LATENCY_STORM')} className={`flex-1 min-w-[100px] px-3 py-2 rounded-lg text-xs font-bold tracking-wide border transition-all duration-200 ${vendorState === 'LATENCY_STORM' ? 'bg-amber-500 text-slate-950 border-amber-400 shadow-md' : 'bg-slate-900 text-slate-400 border-slate-800 hover:text-slate-200'}`}>⏱️ TIMEOUT</button>
            </div>
          </div>

          {/* ========================================================================= */}
          {/* SUB-STEP 5.5: RECONCILIATION MANAGEMENT PANEL                             */}
          {/* ========================================================================= */}
          <div className="border border-slate-900 bg-slate-900/20 rounded-xl p-6 backdrop-blur-sm shadow-xl flex flex-col justify-between">
            <div>
              <h3 className="text-sm font-bold text-white mb-1">Queue Backlog Reconciliation Engine</h3>
              <p className="text-xs text-slate-400 mb-4">Re-enqueue transient failures back to live execution contexts once third-party health is verified.</p>
            </div>
            
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pt-2">
              {/* CHECKBOX CONFIGURATOR */}
              <label className="inline-flex items-center gap-2.5 cursor-pointer select-none">
                <input 
                  type="checkbox" 
                  checked={includeDlq}
                  onChange={(e) => setIncludeDlq(e.target.checked)}
                  className="h-4 w-4 bg-slate-950 border-slate-800 text-indigo-500 rounded focus:ring-0 cursor-pointer accent-indigo-500"
                />
                <div className="flex flex-col">
                  <span className="text-xs font-bold text-slate-200">Resurrect DLQ Records</span>
                  <span className="text-[10px] text-purple-400 font-semibold tracking-tight">Expand scope to Dead Letter Queue</span>
                </div>
              </label>

              {/* ACTION FLUSH SWITCH */}
              <button
                disabled={flushingBacklog}
                onClick={triggerBacklogFlush}
                className={`w-full sm:w-auto px-5 py-2 rounded-lg text-xs font-bold tracking-wide border transition-all duration-200 flex items-center justify-center gap-2 ${
                  flushingBacklog 
                    ? 'bg-indigo-500/20 text-indigo-300 border-indigo-500/30 cursor-not-allowed'
                    : 'bg-indigo-600 hover:bg-indigo-500 text-white border-indigo-400 shadow-lg shadow-indigo-600/10 hover:shadow-indigo-500/20 active:scale-95'
                }`}
              >
                {flushingBacklog ? '🔄 FLUSHING PIPES...' : '⚡ FLUSH BACKLOG QUEUE'}
              </button>
            </div>
          </div>
        </div>

        {/* RECONCILIATION NOTIFICATION TOAST TO DISPLAY OPERATION RESULTS */}
        {reconciliationMessage && (
          <div className={`mb-6 p-3 rounded-lg text-xs font-bold border flex items-center gap-2 animate-bounce ${
            reconciliationMessage.type === 'success' 
              ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30' 
              : 'bg-red-500/10 text-red-400 border-red-500/30'
          }`}>
            {reconciliationMessage.type === 'success' ? '✅' : '❌'} {reconciliationMessage.text}
          </div>
        )}

        {/* REAL-TIME STREAM TABLE */}
        <div className="border border-slate-900 bg-slate-900/20 rounded-xl shadow-2xl overflow-hidden backdrop-blur-sm">
          <div className="px-6 py-4 border-b border-slate-900 bg-slate-900/40">
            <h3 className="text-sm font-bold text-white">Live Ingestion Delivery Log Stream</h3>
            <p className="text-xs text-slate-500 mt-0.5">Exposing the top 10 most recent records stored within the persistent layer.</p>
          </div>
          
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse">
              <thead>
                <tr className="border-b border-slate-900 bg-slate-950/40 text-[10px] font-bold uppercase tracking-wider text-slate-500">
                  <th className="px-6 py-3">Transaction ID</th>
                  <th className="px-6 py-3">Recipient Routing Target</th>
                  <th className="px-6 py-3">Idempotency Key Hash</th>
                  <th className="px-6 py-3 text-center">Retry Overhead</th>
                  <th className="px-6 py-3 text-right">Delivery State</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-900/60 text-xs">
                {notifications.length === 0 ? (
                  <tr>
                    <td colSpan="5" className="px-6 py-12 text-center font-medium text-slate-600 italic">
                      No ingestion vectors registered. Execute payload submissions to establish streaming lines.
                    </td>
                  </tr>
                ) : (
                  notifications.map((note) => (
                    <tr key={note.id} className="hover:bg-slate-900/20 transition-colors duration-150">
                      <td className="px-6 py-3.5 font-mono text-slate-400 text-[11px]">{note.id.substring(0, 8)}...</td>
                      <td className="px-6 py-3.5 font-semibold text-slate-200">{note.recipient}</td>
                      <td className="px-6 py-3.5 font-mono text-slate-500 text-[11px]">{note.idempotency_key}</td>
                      <td className="px-6 py-3.5 text-center font-bold text-slate-300">{note.retry_count}</td>
                      <td className="px-6 py-3.5 text-right">
                        <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-bold border uppercase tracking-wider ${getStatusBadge(note.status)}`}>
                          {note.status}
                        </span>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>

      </main>
    </div>
  );
}