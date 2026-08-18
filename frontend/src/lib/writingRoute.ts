export const WRITING_AREA_VIEWS = {
  blueprint: ["overview", "orientation", "story", "volumes", "chapters"],
  writing: ["chapter", "revision", "agent-suggestion"],
  "auto-book": ["readiness", "runs", "generation-runs", "diagnostics"],
  world: ["library", "factions", "relationships", "curation", "candidates"],
  continuity: [
    "overview",
    "facts",
    "threads",
    "health",
    "state-issues",
    "proposals",
  ],
} as const;

export type WritingArea = keyof typeof WRITING_AREA_VIEWS;
export type WritingView = (typeof WRITING_AREA_VIEWS)[WritingArea][number];

export const WRITING_AREA_DEFAULT_VIEWS = {
  blueprint: "overview",
  writing: "chapter",
  "auto-book": "readiness",
  world: "library",
  continuity: "overview",
} as const satisfies Record<WritingArea, WritingView>;

export const WRITING_TARGET_KEYS = [
  "volume",
  "chapter",
  "scene",
  "cardType",
  "card",
  "candidate",
  "job",
  "event",
  "issue",
  "run",
  "suggestion",
  "visual",
] as const;

const LEGACY_ROUTE_KEYS = new Set(["curateCards", "reviewCards"]);
const OWNED_ROUTE_KEYS = new Set<string>([
  "area",
  "view",
  ...WRITING_TARGET_KEYS,
  ...LEGACY_ROUTE_KEYS,
]);

export type WritingTargetKey = (typeof WRITING_TARGET_KEYS)[number];
export type WritingRouteTargets = Partial<Record<WritingTargetKey, string>>;

export interface WritingRoute {
  area: WritingArea;
  view: WritingView;
  targets: WritingRouteTargets;
}

export type InvalidWritingTarget =
  | {
      kind: "area" | "view";
      value: string;
    }
  | {
      kind: "target";
      key: WritingTargetKey;
      value: string;
    };

export interface ResolvedWritingRoute {
  route: WritingRoute;
  source: "canonical" | "legacy" | "default";
  invalidTarget: InvalidWritingTarget | null;
  /** Replacement query without the leading question mark; null means no rewrite. */
  canonicalSearch: string | null;
}

export const WRITING_REFERENCE_CARD_TYPES = [
  "character",
  "location",
  "item",
  "rule",
  "lore",
] as const;

const REFERENCE_CARD_TYPES = new Set<string>(WRITING_REFERENCE_CARD_TYPES);

const LEGACY_VIEW_ROUTES: Record<
  string,
  Pick<WritingRoute, "area" | "view">
> = {
  "novel-info": { area: "blueprint", view: "overview" },
  "chapter-editor": { area: "writing", view: "chapter" },
  "agent-studio": { area: "writing", view: "revision" },
  "reference-cards": { area: "world", view: "library" },
  "faction-cards": { area: "world", view: "factions" },
  "relationship-map": { area: "world", view: "relationships" },
  "plot-threads": { area: "continuity", view: "threads" },
  "character-memory": { area: "continuity", view: "facts" },
  "story-health": { area: "continuity", view: "health" },
  "generation-runs": { area: "auto-book", view: "generation-runs" },
};

export function legacyWritingRouteSignal(
  search: URLSearchParams,
): string | null {
  const legacyView = search.get("view");
  if (legacyView && Object.hasOwn(LEGACY_VIEW_ROUTES, legacyView)) {
    return `view:${legacyView}`;
  }
  if (search.get("reviewCards") === "1") return "reviewCards";
  if (search.get("curateCards") === "1") return "curateCards";
  const cardType = search.get("cardType");
  if (cardType && REFERENCE_CARD_TYPES.has(cardType)) {
    return `cardType:${cardType}`;
  }
  return null;
}

const VIEW_TARGETS: Record<WritingArea, Record<string, readonly WritingTargetKey[]>> = {
  blueprint: {
    overview: [],
    orientation: [],
    story: [],
    volumes: ["volume"],
    chapters: ["volume", "chapter"],
  },
  writing: {
    chapter: ["volume", "chapter", "scene", "run", "visual"],
    revision: ["chapter", "run", "suggestion"],
    "agent-suggestion": ["chapter", "scene", "run", "suggestion"],
  },
  "auto-book": {
    readiness: ["volume"],
    runs: ["volume", "job"],
    "generation-runs": ["chapter", "job", "event", "run"],
    diagnostics: ["chapter", "job", "event"],
  },
  world: {
    library: ["cardType", "card"],
    factions: ["card"],
    relationships: ["card"],
    curation: ["cardType", "card"],
    candidates: ["cardType", "candidate"],
  },
  continuity: {
    overview: ["volume", "chapter", "issue"],
    facts: ["card", "chapter", "issue", "run", "suggestion"],
    threads: ["chapter", "issue", "run", "suggestion"],
    health: ["volume", "chapter", "issue"],
    "state-issues": ["chapter", "issue", "run"],
    proposals: ["chapter", "issue", "run", "suggestion"],
  },
};

function isWritingArea(value: string | null): value is WritingArea {
  return value !== null && Object.hasOwn(WRITING_AREA_VIEWS, value);
}

function isWritingView(area: WritingArea, value: string): value is WritingView {
  return (WRITING_AREA_VIEWS[area] as readonly string[]).includes(value);
}

function normalizedTargetValue(
  key: WritingTargetKey,
  value: string | null | undefined,
): string | null {
  const normalized = value?.trim() ?? "";
  if (!normalized || normalized.length > 200) return null;
  if (key === "scene") {
    if (!/^(0|[1-9]\d*)$/.test(normalized)) return null;
    if (!Number.isSafeInteger(Number(normalized))) return null;
  }
  if (key === "cardType" && !REFERENCE_CARD_TYPES.has(normalized)) return null;
  if (key === "visual" && !["cover", "portrait", "scene"].includes(normalized)) {
    return null;
  }
  return normalized;
}

function collectTargets(
  search: URLSearchParams,
  area: WritingArea,
  view: WritingView,
  updates: WritingRouteTargets = {},
): WritingRouteTargets {
  const allowed = new Set(VIEW_TARGETS[area][view] ?? []);
  const targets: WritingRouteTargets = {};
  for (const key of WRITING_TARGET_KEYS) {
    if (!allowed.has(key)) continue;
    const candidate = Object.hasOwn(updates, key)
      ? updates[key]
      : search.get(key);
    const value = normalizedTargetValue(key, candidate);
    if (value !== null) targets[key] = value;
  }
  return targets;
}

function invalidTargetFromSearch(
  search: URLSearchParams,
  area: WritingArea,
  view: WritingView,
): InvalidWritingTarget | null {
  const allowed = new Set(VIEW_TARGETS[area][view] ?? []);
  for (const key of WRITING_TARGET_KEYS) {
    if (!allowed.has(key) || !search.has(key)) continue;
    const rawValue = search.get(key) ?? "";
    if (normalizedTargetValue(key, rawValue) === null) {
      return { kind: "target", key, value: rawValue };
    }
  }
  return null;
}

function invalidTargetDependency(
  route: WritingRoute,
): Extract<InvalidWritingTarget, { kind: "target" }> | null {
  const { targets } = route;
  if (targets.scene && !targets.chapter) {
    return { kind: "target", key: "scene", value: targets.scene };
  }
  if (targets.event && !targets.job) {
    return { kind: "target", key: "event", value: targets.event };
  }
  if (targets.suggestion && !targets.run) {
    return {
      kind: "target",
      key: "suggestion",
      value: targets.suggestion,
    };
  }
  if (
    route.area === "writing" &&
    route.view === "chapter" &&
    targets.run &&
    !targets.chapter
  ) {
    return { kind: "target", key: "run", value: targets.run };
  }
  if (targets.visual === "scene" && !targets.chapter) {
    return { kind: "target", key: "visual", value: targets.visual };
  }
  if (targets.visual === "portrait" && !targets.card) {
    return { kind: "target", key: "visual", value: targets.visual };
  }
  return null;
}

function serializeRoute(
  route: WritingRoute,
  preservedFrom?: URLSearchParams,
): string {
  const next = new URLSearchParams();
  next.set("area", route.area);
  next.set("view", route.view);
  for (const key of WRITING_TARGET_KEYS) {
    const value = route.targets[key];
    if (value) next.set(key, value);
  }
  for (const [key, value] of preservedFrom?.entries() ?? []) {
    if (!OWNED_ROUTE_KEYS.has(key)) next.append(key, value);
  }
  return next.toString();
}

function resolved(
  route: WritingRoute,
  source: ResolvedWritingRoute["source"],
  original: URLSearchParams,
): ResolvedWritingRoute {
  const canonical = serializeRoute(route, original);
  return {
    route,
    source,
    invalidTarget: null,
    canonicalSearch: canonical === original.toString() ? null : canonical,
  };
}

function invalid(
  fallback: Pick<WritingRoute, "area" | "view">,
  kind: "area" | "view",
  value: string,
): ResolvedWritingRoute {
  return {
    route: { ...fallback, targets: {} },
    source: "canonical",
    invalidTarget: { kind, value },
    canonicalSearch: null,
  };
}

function invalidRouteTarget(
  area: WritingArea,
  target: Extract<InvalidWritingTarget, { kind: "target" }>,
): ResolvedWritingRoute {
  return {
    route: {
      area,
      view: WRITING_AREA_DEFAULT_VIEWS[area],
      targets: {},
    },
    source: "canonical",
    invalidTarget: target,
    canonicalSearch: null,
  };
}

function resolveRoute(
  route: WritingRoute,
  source: ResolvedWritingRoute["source"],
  original: URLSearchParams,
): ResolvedWritingRoute {
  const dependencyFailure = invalidTargetDependency(route);
  return dependencyFailure
    ? invalidRouteTarget(route.area, dependencyFailure)
    : resolved(route, source, original);
}

function legacyRoute(
  search: URLSearchParams,
  fallback: Pick<WritingRoute, "area" | "view">,
): ResolvedWritingRoute {
  const legacyView = search.get("view");
  let area: WritingArea;
  let view: WritingView;

  if (legacyView !== null) {
    const mapped = LEGACY_VIEW_ROUTES[legacyView];
    if (!mapped) return invalid(fallback, "view", legacyView);
    area = mapped.area;
    view = mapped.view;
  } else if (search.get("reviewCards") === "1") {
    area = "world";
    view = "candidates";
  } else if (search.get("curateCards") === "1") {
    area = "world";
    view = "curation";
  } else if (search.has("cardType")) {
    const rawCardType = search.get("cardType") ?? "";
    if (normalizedTargetValue("cardType", rawCardType) === null) {
      return invalidRouteTarget("world", {
        kind: "target",
        key: "cardType",
        value: rawCardType,
      });
    }
    area = "world";
    view = "library";
  } else {
    const targetFailure = invalidTargetFromSearch(
      search,
      fallback.area,
      fallback.view,
    );
    if (targetFailure?.kind === "target") {
      return invalidRouteTarget(fallback.area, targetFailure);
    }
    return resolveRoute(
      {
        ...fallback,
        targets: collectTargets(search, fallback.area, fallback.view),
      },
      "default",
      search,
    );
  }

  const targetFailure = invalidTargetFromSearch(search, area, view);
  if (targetFailure?.kind === "target") {
    return invalidRouteTarget(area, targetFailure);
  }

  const route: WritingRoute = {
    area,
    view,
    targets: collectTargets(search, area, view),
  };
  if (
    area === "world" &&
    ["library", "curation", "candidates"].includes(view) &&
    !route.targets.cardType
  ) {
    route.targets.cardType = "character";
  }
  return resolveRoute(route, "legacy", search);
}

export function resolveWritingRoute(
  search: URLSearchParams,
  fallback: Pick<WritingRoute, "area" | "view">,
): ResolvedWritingRoute {
  const requestedArea = search.get("area");
  if (requestedArea === null) return legacyRoute(search, fallback);
  if (!isWritingArea(requestedArea)) {
    return invalid(fallback, "area", requestedArea);
  }

  const requestedView =
    search.get("view") ?? WRITING_AREA_DEFAULT_VIEWS[requestedArea];
  if (!isWritingView(requestedArea, requestedView)) {
    return invalid(
      {
        area: requestedArea,
        view: WRITING_AREA_DEFAULT_VIEWS[requestedArea],
      },
      "view",
      requestedView,
    );
  }

  const targetFailure = invalidTargetFromSearch(
    search,
    requestedArea,
    requestedView,
  );
  if (targetFailure?.kind === "target") {
    return invalidRouteTarget(requestedArea, targetFailure);
  }

  return resolveRoute(
    {
      area: requestedArea,
      view: requestedView,
      targets: collectTargets(search, requestedArea, requestedView),
    },
    "canonical",
    search,
  );
}

export function defaultWritingRoute(
  chapterCount: number,
): Pick<WritingRoute, "area" | "view"> {
  return chapterCount > 0
    ? { area: "writing", view: "chapter" }
    : { area: "blueprint", view: "overview" };
}

export function buildAreaSearch(
  _current: URLSearchParams,
  area: WritingArea,
): string {
  return serializeRoute({
    area,
    view: WRITING_AREA_DEFAULT_VIEWS[area],
    targets: {},
  }, _current);
}

export function buildViewSearch(
  current: URLSearchParams,
  area: WritingArea,
  view: WritingView,
  updates: WritingRouteTargets = {},
): string {
  if (!isWritingView(area, view)) {
    throw new Error(`Unsupported writing view: ${area}/${view}`);
  }
  return serializeRoute(
    {
      area,
      view,
      targets: collectTargets(current, area, view, updates),
    },
    current,
  );
}
