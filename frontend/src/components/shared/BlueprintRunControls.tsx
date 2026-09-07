"use client";

import { useEffect, useId, useRef, useState } from "react";
import { Button } from "@heroui/react";
import { useTranslations } from "next-intl";
import { apiGet, apiPost, apiPostSSE, SSEError } from "@/lib/api";
import { blueprintGenerationStream } from "@/lib/generationStreamContracts";
import { blueprintExecutionRef, blueprintInputIdentity, blueprintAuthorIdentity, buildBlueprintStartRequest, buildBlueprintResumeRequest, type BlueprintReadiness, type BlueprintRun, type BlueprintRunRequest, type BlueprintRunSummary } from "@/lib/blueprintRunClient";
import type { AICreateResponse, BlueprintExecutionRef } from "@/types/novel";

type Props = {
  entry: "create-novel-by-ai" | "regenerate-blueprint";
  request: BlueprintRunRequest;
  initialRunId?: string;
  disabled?: boolean;
  validateInput?: () => boolean;
  onBusyChange?: (busy: boolean) => void;
  onBound?: (execution: BlueprintExecutionRef, request: BlueprintRunRequest) => void;
  onRead?: (run: BlueprintRun) => void;
  onRestoreInput?: (run: BlueprintRun) => void;
  onEvent?: (event: string, data: Record<string, unknown>) => void;
  onComplete: (result: AICreateResponse, request: BlueprintRunRequest, execution: BlueprintExecutionRef) => void;
};

/** Both entry points use the same explicit authorization and read-only recovery UI. */
export default function BlueprintRunControls(props: Props) {
  const t = useTranslations("blueprintRun");
  const tBudget = useTranslations("writing.novelInfo.regeneration");
  const tStream = useTranslations("streamErrors");
  const id = useId();
  const callbacks = useRef(props);
  useEffect(() => { callbacks.current = props; });
  const [budget, setBudget] = useState("");
  const [reuse, setReuse] = useState(false);
  const [preview, setPreview] = useState<{ report: BlueprintReadiness; request: BlueprintRunRequest; identity: string } | null>(null);
  const [run, setRun] = useState<BlueprintRun | null>(null);
  const [history, setHistory] = useState<BlueprintRunSummary[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [executing, setExecuting] = useState(false);
  const executingRunId = useRef<string | null>(null);
  const mounted = useRef(true);
  const pauseController = useRef<AbortController | null>(null);
  const [error, setError] = useState("");
  const [ackBudget, setAckBudget] = useState(false);
  const [ackUnknown, setAckUnknown] = useState(false);
  const active = useRef<AbortController | null>(null);
  const runRef = useRef<BlueprintRun | null>(null);
  const inputIdentity = blueprintInputIdentity(props.request);
  const ready = preview?.identity === inputIdentity ? preview : null;
  const sameAuthor = !!run && blueprintAuthorIdentity(run.request) === blueprintAuthorIdentity(props.request);
  const sameInput = !!run && blueprintInputIdentity(run.request) === inputIdentity;
  const parsedBudget = budget === "" ? null : /^\d+$/.test(budget) ? Number(budget) : NaN;
  const validBudget = parsedBudget === null || (Number.isSafeInteger(parsedBudget) && parsedBudget > 0);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; active.current?.abort(); active.current = null; pauseController.current?.abort(); };
  }, []);
  useEffect(() => {
    active.current?.abort(); active.current = null;
    setBusy(false); setExecuting(false); executingRunId.current = null; setAckBudget(false); setAckUnknown(false);
    callbacks.current.onBusyChange?.(false);
  }, [inputIdentity]);

  const setCurrentRun = (value: BlueprintRun) => {
    runRef.current = value;
    setRun(value);
    callbacks.current.onRead?.(value);
    setPreview(value.status === "ready" ? { report: value.readiness, request: value.request, identity: blueprintInputIdentity(value.request) } : null);
    if (value.status === "ready") setBudget(value.request.token_budget == null ? "" : String(value.request.token_budget));
  };

  const begin = () => {
    if (active.current) return null;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    return controller;
  };
  const current = (controller: AbortController) => active.current === controller && !controller.signal.aborted;
  const finish = (controller: AbortController) => {
    if (active.current === controller) { active.current = null; setBusy(false); }
  };
  const showError = (cause: unknown) => setError(cause instanceof SSEError ? tStream(cause.code) : cause instanceof Error ? cause.message : t("failed"));

  // Mounting and restoring only read. Never resume or issue readiness implicitly.
  useEffect(() => {
    const runId = props.initialRunId;
    if (!runId || runRef.current?.run_id === runId || active.current) return;
    const controller = new AbortController();
    apiGet<BlueprintRun>("/api/llm/blueprint-runs/" + encodeURIComponent(runId), { signal: controller.signal })
      .then((value) => {
        if (controller.signal.aborted || active.current) return;
        runRef.current = value;
        setRun(value);
        callbacks.current.onRead?.(value);
        if (value.status === "ready") {
          setPreview({ report: value.readiness, request: value.request, identity: blueprintInputIdentity(value.request) });
          setBudget(value.request.token_budget == null ? "" : String(value.request.token_budget));
        }
      })
      .catch((cause) => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : String(cause)); });
    return () => controller.abort();
  }, [props.initialRunId]);

  const read = async (runId: string) => {
    const controller = begin();
    if (!controller) return;
    try {
      const value = await apiGet<BlueprintRun>("/api/llm/blueprint-runs/" + encodeURIComponent(runId), { signal: controller.signal });
      if (current(controller)) { setCurrentRun(value); setReuse(false); }
    } catch (cause) { if (current(controller)) showError(cause); }
    finally { finish(controller); }
  };

  const findRuns = async () => {
    const controller = begin();
    if (!controller) return;
    try {
      const draftId = props.request.draft_id;
      const value = await apiGet<{ data: BlueprintRunSummary[] }>("/api/llm/blueprint-runs" + (draftId ? "?draft_id=" + encodeURIComponent(draftId) : ""), { signal: controller.signal });
      if (current(controller)) setHistory(value.data);
    } catch (cause) { if (current(controller)) showError(cause); }
    finally { finish(controller); }
  };

  const inspect = async () => {
    if (props.disabled || !validBudget || (props.validateInput && !props.validateInput())) return;
    const controller = begin();
    if (!controller) return;
    setPreview(null); setAckBudget(false); setAckUnknown(false);
    const request: BlueprintRunRequest = {
      ...props.request, token_budget: parsedBudget, allow_failure_retry: false,
      ...(reuse && sameAuthor && run && { draft_id: run.draft_id, reuse_run_id: run.run_id }),
    };
    try {
      const report = await apiPost<BlueprintReadiness>("/api/llm/" + props.entry + "/readiness", request, { signal: controller.signal });
      if (!current(controller)) return;
      const sealed = { ...request, draft_id: report.draft_id };
      setPreview({ report, request: sealed, identity: inputIdentity });
      callbacks.current.onBound?.(blueprintExecutionRef(report), sealed);
      if (report.status === "blocked") setError(tBudget("readinessBlocked"));
    } catch (cause) { if (current(controller)) showError(cause); }
    finally { finish(controller); }
  };

  const execute = async (resume: boolean) => {
    if (props.disabled || (props.validateInput && !props.validateInput())) return;
    const source = resume ? run : null;
    if (resume && (!source || !sameInput)) return;
    if (!resume && (!ready || ready.report.status === "blocked" || (ready.report.uses_system_token_budget && !ackBudget) || (ready.report.uncertain_source && !ackUnknown))) return;
    const report = source?.readiness ?? ready!.report;
    const request = source?.request ?? ready!.request;
    const execution = blueprintExecutionRef(source ?? report);
    const controller = begin();
    if (!controller) return;
    setExecuting(true); executingRunId.current = execution.run_id;
    callbacks.current.onBusyChange?.(true);
    let result: AICreateResponse | null = null;
    let failure = "";
    try {
      await apiPostSSE(resume ? "/api/llm/blueprint-runs/" + execution.run_id + "/resume" : "/api/llm/" + props.entry,
        resume ? buildBlueprintResumeRequest(execution) : buildBlueprintStartRequest(request, report, ackBudget, ackUnknown), (event, data) => {
          if (!current(controller)) return;
          callbacks.current.onEvent?.(event, data);
          if (event === "done") {
            if (data.success && data.result) result = data.result as AICreateResponse;
            else failure = typeof data.error === "string" ? data.error : t("failed");
          }
        }, { ...blueprintGenerationStream, signal: controller.signal });
      if (!current(controller)) return;
      if (failure) setError(failure);
    } catch (cause) { if (current(controller)) showError(cause); }
    finally {
      if (current(controller)) {
        setPreview(null);
        try {
          const value = await apiGet<BlueprintRun>("/api/llm/blueprint-runs/" + execution.run_id, { signal: controller.signal });
          if (current(controller)) setCurrentRun(value);
        } catch { /* A terminal result remains usable; history supports later reconciliation. */ }
        if (current(controller)) {
          setExecuting(false); executingRunId.current = null;
          callbacks.current.onBusyChange?.(false);
          if (result) callbacks.current.onComplete(result, request, execution);
        }
      }
      finish(controller);
    }
  };

  const pause = async () => {
    const runId = executingRunId.current ?? run?.run_id;
    if (!runId || pauseController.current) return;
    const controller = new AbortController(); pauseController.current = controller;
    // The server stops at its next safe boundary and records in-flight uncertainty.
    try {
      const value = await apiPost<BlueprintRun>("/api/llm/blueprint-runs/" + runId + "/pause", {}, { signal: controller.signal });
      if (!mounted.current || controller.signal.aborted) return;
      if (active.current) setCurrentRun(value);
      else await read(runId);
    } catch (cause) { if (mounted.current && !controller.signal.aborted) showError(cause); }
    finally { if (pauseController.current === controller) pauseController.current = null; }
  };

  return <section className="space-y-4 border-t border-border pt-4" aria-label={t("title")}>
    <div><h3 className="text-sm font-semibold">{t("title")}</h3><p className="mt-1 text-xs leading-5 text-muted">{t("description")}</p></div>
    {run && <div className="space-y-3 rounded-lg border border-border p-3 text-sm">
      <p className="font-medium">{t("statusLabel", { status: t("status." + run.status), count: run.completed_steps.length })}</p>
      <p className="text-xs text-muted">{t("spent", { calls: run.cumulative_calls_used, tokens: run.cumulative_tokens_used.toLocaleString(), reserved: run.tokens_reserved.toLocaleString() })}</p>
      {(run.has_uncertain || run.status === "uncertain") && <p className="text-xs leading-5 text-warning">{t("uncertain")}</p>}
      {!sameInput && <p className="text-xs leading-5 text-muted">{t("differentInput")}</p>}
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="secondary" isDisabled={busy} onPress={() => void read(run.run_id)}>{t("refresh")}</Button>
        {!sameInput && props.onRestoreInput && JSON.stringify(run.request.card_imports ?? []) === JSON.stringify(props.request.card_imports ?? []) && <Button size="sm" variant="secondary" isDisabled={busy} onPress={() => { callbacks.current.onRestoreInput?.(run); callbacks.current.onBound?.(blueprintExecutionRef(run), run.request); }}>{t("restoreInput")}</Button>}
        {sameInput && run.status === "completed" && run.result && <Button size="sm" isDisabled={busy || props.disabled} onPress={() => props.onComplete(run.result!, run.request, blueprintExecutionRef(run))}>{t("openResult")}</Button>}
        {sameInput && (run.status === "paused" || run.status === "running") && <Button size="sm" isDisabled={busy || props.disabled} onPress={() => void execute(true)}>{t("resume")}</Button>}
        {run.status === "running" && !busy && <Button size="sm" variant="secondary" onPress={() => void pause()}>{t("pause")}</Button>}
      </div>
      {sameAuthor && run.completed_steps.length > 0 && run.status !== "running" && <label className="flex items-start gap-2 text-xs leading-5"><input type="checkbox" checked={reuse} disabled={busy} onChange={(e) => { setReuse(e.target.checked); setPreview(null); }} className="mt-1 shrink-0"/><span>{t("reuse")}</span></label>}
    </div>}
    <label className="block text-xs font-medium" htmlFor={id}>{tBudget("tokenBudgetLabel")}</label>
    <input id={id} type="number" min={1} step={1} value={budget} disabled={busy} aria-invalid={!validBudget} onChange={(e) => { setBudget(e.target.value); setPreview(null); setAckBudget(false); }} className="min-h-10 w-full rounded-md border border-border bg-background px-3 py-2 text-base" />
    <p className="text-xs leading-5 text-muted">{tBudget("tokenBudgetHint")}</p>
    {!validBudget && <p role="note" className="text-xs text-danger">{tBudget("tokenBudgetInvalid")}</p>}
    {ready && <div className="space-y-3 border-t border-border pt-3">
      <dl className="grid grid-cols-2 gap-3 text-xs"><div><dt className="text-muted">{tBudget("maximumCalls")}</dt><dd className="mt-1 font-semibold">{ready.report.maximum_provider_attempts}</dd></div><div><dt className="text-muted">{tBudget("conservativeMaximum")}</dt><dd className="mt-1 font-semibold">{ready.report.maximum_tokens_total.toLocaleString()}</dd></div></dl>
      <ul className="space-y-1 break-words text-xs text-muted">{ready.report.providers.map((provider) => <li key={provider.step}>{provider.provider_alias} · {provider.provider_model}</li>)}</ul>
      {ready.report.source_summary && <p className="text-xs text-muted">{t("sourceCost", { calls: ready.report.source_summary.calls_used, tokens: ready.report.source_summary.tokens_used.toLocaleString(), steps: ready.report.reused_steps.length })}</p>}
      {!ready.report.budget_covers_conservative_maximum && <p className="text-xs leading-5 text-warning">{tBudget("budgetMayStop")}</p>}
      {ready.report.uses_system_token_budget && <label className="flex items-start gap-2 text-xs leading-5"><input type="checkbox" checked={ackBudget} disabled={busy} onChange={(e) => setAckBudget(e.target.checked)} className="mt-1 shrink-0"/><span>{tBudget("automaticBudgetNotice", { budget: (ready.report.token_budget ?? 0).toLocaleString() })} {tBudget("automaticBudgetConfirm")}</span></label>}
      {ready.report.uncertain_source && <label className="flex items-start gap-2 text-xs leading-5"><input type="checkbox" checked={ackUnknown} disabled={busy} onChange={(e) => setAckUnknown(e.target.checked)} className="mt-1 shrink-0"/><span>{t("ackUnknown")}</span></label>}
    </div>}
    {error && <p role="alert" className="break-words rounded-lg border border-danger/30 bg-danger/5 p-3 text-sm text-danger">{error}</p>}
    <div className="flex flex-wrap gap-2">
      <Button variant="primary" isDisabled={busy || props.disabled || !validBudget} onPress={() => void inspect()}>{executing ? t("status.running") : busy ? tBudget("checkingReadiness") : tBudget("checkReadiness")}</Button>
      {ready && ready.report.status !== "blocked" && <Button variant="primary" isDisabled={busy || props.disabled || (ready.report.uses_system_token_budget && !ackBudget) || (ready.report.uncertain_source && !ackUnknown)} onPress={() => void execute(false)}>{tBudget("confirmPaidCall")}</Button>}
      {executing && <Button variant="secondary" onPress={() => void pause()}>{t("pause")}</Button>}
      <Button variant="ghost" isDisabled={busy} onPress={() => void findRuns()}>{t("history")}</Button>
    </div>
    {history && <ul className="space-y-2 text-xs">{history.length === 0 && <li>{t("emptyHistory")}</li>}{history.map((item) => <li key={item.run_id} className="flex min-w-0 flex-wrap items-center justify-between gap-2 border-t border-border pt-2"><span>{new Date(item.created_at).toLocaleString()} · {t("status." + item.status)} · {item.completed_steps.length}</span><Button size="sm" variant="ghost" isDisabled={busy} onPress={() => void read(item.run_id)}>{t("inspectRun")}</Button></li>)}</ul>}
  </section>;
}
