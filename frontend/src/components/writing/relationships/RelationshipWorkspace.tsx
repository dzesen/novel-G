"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { apiGet } from "@/lib/api";
import type { CoreFaction, FactionRelation, FactionRelationType } from "@/types/novel";

interface RelationshipWorkspaceProps {
  mode: "create" | "edit";
  novelId?: string;
}

const EDGE_COLORS: Record<FactionRelationType, string> = {
  hostile: "#b91c1c",
  allied: "#3f7a4d",
  cold_war: "#64748b",
  dependent: "#8b5cf6",
  subordinate: "#2563eb",
  trade_partner: "#b7791f",
  secret_cooperation: "#0f766e",
  historical_enemy: "#9f1239",
};

export default function RelationshipWorkspace({ mode, novelId }: RelationshipWorkspaceProps) {
  const t = useTranslations("writing.relationshipMap");
  const tf = useTranslations("writing.factions");
  const [factions, setFactions] = useState<CoreFaction[]>([]);
  const [relations, setRelations] = useState<FactionRelation[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const relationLabels: Record<FactionRelationType, string> = useMemo(() => ({
    hostile: tf("relationTypes.hostile"),
    allied: tf("relationTypes.allied"),
    cold_war: tf("relationTypes.coldWar"),
    dependent: tf("relationTypes.dependent"),
    subordinate: tf("relationTypes.subordinate"),
    trade_partner: tf("relationTypes.tradePartner"),
    secret_cooperation: tf("relationTypes.secretCooperation"),
    historical_enemy: tf("relationTypes.historicalEnemy"),
  }), [tf]);

  const load = useCallback(async () => {
    if (mode !== "edit" || !novelId) return;
    setLoading(true);
    setError(null);
    try {
      const [factionResponse, relationResponse] = await Promise.all([
        apiGet<{ data: CoreFaction[] }>(`/api/factions/novel/${novelId}`),
        apiGet<{ data: FactionRelation[] }>(`/api/faction-relations/novel/${novelId}`),
      ]);
      setFactions(factionResponse.data);
      setRelations(relationResponse.data);
      setSelectedId(relationResponse.data[0]?.relation_id ?? relationResponse.data[0]?._id ?? null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [mode, novelId, t]);

  useEffect(() => { void load(); }, [load]);

  const nodes = useMemo(() => {
    const centerX = 430;
    const centerY = 280;
    const radiusX = Math.min(315, 125 + factions.length * 20);
    const radiusY = Math.min(205, 90 + factions.length * 14);
    return factions.map((faction, index) => {
      const angle = (Math.PI * 2 * index) / Math.max(factions.length, 1) - Math.PI / 2;
      return {
        faction,
        x: centerX + Math.cos(angle) * radiusX,
        y: centerY + Math.sin(angle) * radiusY,
      };
    });
  }, [factions]);

  const nodeById = useMemo(() => new Map(nodes.map((node) => [node.faction.faction_id, node])), [nodes]);
  const selectedRelation = relations.find((relation) => (relation.relation_id ?? relation._id) === selectedId) ?? null;

  if (mode !== "edit" || !novelId) {
    return <Empty title={t("title")} description={t("createModeUnavailable")} />;
  }

  return (
    <div className="flex h-full min-h-0 flex-col bg-background">
      <header className="flex flex-wrap items-start justify-between gap-4 border-b border-border bg-surface px-5 py-4 sm:px-7">
        <div>
          <h1 className="text-lg font-semibold text-foreground">{t("title")}</h1>
          <p className="mt-1 text-sm text-muted">{t("subtitle")}</p>
        </div>
        <button type="button" onClick={() => void load()} className="rounded-lg border border-border px-3 py-2 text-sm font-medium text-foreground transition-colors hover:bg-surface-secondary">{t("refresh")}</button>
      </header>

      {error ? (
        <div className="m-5 rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200">{error}</div>
      ) : loading ? (
        <Empty title={t("loading")} description={t("loadingDescription")} />
      ) : !factions.length ? (
        <Empty title={t("emptyTitle")} description={t("emptyDescription")} />
      ) : (
        <div className="flex min-h-0 flex-1 flex-col xl:flex-row">
          <div className="min-h-[420px] flex-1 overflow-auto p-4 sm:p-6">
            <div className="mx-auto min-w-[760px] max-w-5xl overflow-hidden rounded-xl border border-border bg-surface">
              <svg viewBox="0 0 860 560" role="img" aria-label={t("graphLabel")} className="block h-auto w-full">
                <defs>
                  {Object.entries(EDGE_COLORS).map(([type, color]) => (
                    <marker key={type} id={`arrow-${type}`} viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
                      <path d="M 0 0 L 10 5 L 0 10 z" fill={color} />
                    </marker>
                  ))}
                </defs>
                <rect width="860" height="560" fill="transparent" />
                {relations.map((relation) => {
                  const source = nodeById.get(relation.source_faction_id);
                  const target = nodeById.get(relation.target_faction_id);
                  if (!source || !target) return null;
                  const key = relation.relation_id ?? relation._id ?? `${relation.source_faction_id}-${relation.target_faction_id}`;
                  const selected = key === selectedId;
                  const color = EDGE_COLORS[relation.relation_type] ?? "#64748b";
                  return (
                    <g key={key} onClick={() => setSelectedId(key)} className="cursor-pointer">
                      <line x1={source.x} y1={source.y} x2={target.x} y2={target.y} stroke="transparent" strokeWidth="16" />
                      <line x1={source.x} y1={source.y} x2={target.x} y2={target.y} stroke={color} strokeWidth={selected ? 4 : Math.max(1.5, relation.intensity * 0.55)} strokeOpacity={selected ? 1 : 0.62} markerEnd={`url(#arrow-${relation.relation_type})`} />
                    </g>
                  );
                })}
                {nodes.map(({ faction, x, y }) => (
                  <g key={faction.faction_id ?? faction._id ?? faction.name} transform={`translate(${x} ${y})`}>
                    <circle r="42" fill="var(--color-surface-secondary)" stroke="var(--color-accent)" strokeWidth="2" />
                    <text textAnchor="middle" dominantBaseline="middle" fill="var(--foreground)" fontSize="13" fontWeight="600">
                      {faction.name.length > 8 ? `${faction.name.slice(0, 8)}…` : faction.name}
                    </text>
                  </g>
                ))}
              </svg>
            </div>
          </div>

          <aside className="max-h-[42vh] w-full shrink-0 overflow-y-auto border-t border-border bg-surface p-4 xl:max-h-none xl:w-96 xl:border-l xl:border-t-0">
            <div className="mb-4 flex items-center justify-between">
              <h2 className="font-semibold text-foreground">{t("relations", { count: relations.length })}</h2>
              <span className="text-xs text-muted">{t("clickHint")}</span>
            </div>
            {relations.length ? relations.map((relation) => {
              const key = relation.relation_id ?? relation._id ?? `${relation.source_faction_id}-${relation.target_faction_id}`;
              const sourceName = relation.source_faction_name ?? nodeById.get(relation.source_faction_id)?.faction.name ?? relation.source_faction_id;
              const targetName = relation.target_faction_name ?? nodeById.get(relation.target_faction_id)?.faction.name ?? relation.target_faction_id;
              return (
                <button key={key} type="button" onClick={() => setSelectedId(key)} className={`mb-2 w-full rounded-lg border p-3 text-left transition-colors ${key === selectedId ? "border-accent bg-accent/8" : "border-border hover:bg-surface-secondary"}`}>
                  <div className="flex items-center justify-between gap-2 text-sm font-medium text-foreground"><span>{sourceName} → {targetName}</span><span className="shrink-0 text-xs" style={{ color: EDGE_COLORS[relation.relation_type] }}>{relationLabels[relation.relation_type]}</span></div>
                  <p className="mt-2 line-clamp-2 text-xs leading-5 text-muted">{relation.current_state || relation.core_conflict || t("noDescription")}</p>
                </button>
              );
            }) : <p className="py-10 text-center text-sm text-muted">{t("noRelations")}</p>}

            {selectedRelation && (
              <div className="mt-4 border-t border-border pt-4">
                <p className="text-xs font-semibold uppercase tracking-wider text-muted">{t("detail")}</p>
                <p className="mt-2 text-sm leading-6 text-foreground">{selectedRelation.core_conflict || selectedRelation.current_state || t("noDescription")}</p>
                <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-surface-secondary"><div className="h-full rounded-full bg-accent" style={{ width: `${Math.max(1, Math.min(5, selectedRelation.intensity)) * 20}%` }} /></div>
                <p className="mt-1 text-xs text-muted">{t("intensity", { value: selectedRelation.intensity })}</p>
              </div>
            )}
          </aside>
        </div>
      )}
    </div>
  );
}

function Empty({ title, description }: { title: string; description: string }) {
  return (
    <div className="flex min-h-80 flex-1 items-center justify-center px-6 text-center">
      <div className="max-w-md"><h2 className="text-lg font-semibold text-foreground">{title}</h2><p className="mt-2 text-sm leading-6 text-muted">{description}</p></div>
    </div>
  );
}
