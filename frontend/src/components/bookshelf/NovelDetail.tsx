/* eslint-disable @next/next/no-img-element */
"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { useRouter, usePathname } from "next/navigation";
import { Card, Button, Chip } from "@heroui/react";
import { getImageUrl } from "@/lib/api";
import type { NovelDetail } from "@/types/novel";

interface NovelDetailPanelProps {
  novel: NovelDetail | null;
  onDelete: (id: string) => void;
  onBack: () => void;
}

const STATUS_COLOR_MAP: Record<string, "primary" | "secondary" | "tertiary" | "soft"> = {
  draft: "secondary",
  ongoing: "primary",
  completed: "tertiary",
  paused: "soft",
};

const PREVIEW_LONG_FIELDS: { key: keyof NovelDetail; labelKey: string }[] = [
  { key: "introduction", labelKey: "introduction" },
  { key: "summary", labelKey: "summary" },
  { key: "tone", labelKey: "tone" },
  { key: "target_audience", labelKey: "targetAudience" },
];

const STYLE_THREE_COLS: { key: keyof NovelDetail; labelKey: string }[] = [
  { key: "writing_style", labelKey: "writingStyle" },
  { key: "narrative_pov", labelKey: "narrativePov" },
  { key: "era_background", labelKey: "eraBackground" },
];

export default function NovelDetailPanel({
  novel,
  onDelete,
  onBack,
}: NovelDetailPanelProps) {
  const t = useTranslations("novel");
  const tb = useTranslations("bookshelf");
  const router = useRouter();
  const pathname = usePathname();
  const locale = pathname.startsWith("/en") ? "en" : "zh";
  const [failedManagedCoverId, setFailedManagedCoverId] = useState<
    string | null
  >(null);
  const managedCoverId = novel?.cover_asset_id
    ? String(novel.cover_asset_id)
    : "";
  const coverUrl = managedCoverId
    ? `/api/image-assets/${encodeURIComponent(managedCoverId)}/content`
    : novel?.cover_image;

  const managedCoverFailed =
    Boolean(managedCoverId) &&
    failedManagedCoverId === managedCoverId;

  const mobileBack = (
    <div className="shrink-0 border-b border-border bg-surface px-1 py-1 md:hidden">
      <button
        type="button"
        onClick={onBack}
        className="inline-flex min-h-11 items-center gap-2 rounded-lg px-3 text-sm font-medium text-muted transition-colors hover:bg-surface-secondary hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      >
        <svg
          aria-hidden="true"
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="m15 18-6-6 6-6" />
        </svg>
        {tb("backToShelf")}
      </button>
    </div>
  );

  if (!novel) {
    return (
      <div className="flex h-full min-w-0 flex-col">
        {mobileBack}
        <div className="flex min-h-0 flex-1 items-center justify-center">
          <div className="text-center text-muted">
          <svg
            xmlns="http://www.w3.org/2000/svg"
            width="48"
            height="48"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="mx-auto mb-4 opacity-30"
          >
            <path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H19a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H6.5a1 1 0 0 1 0-5H20" />
          </svg>
          <p className="text-sm">{tb("noSelection")}</p>
        </div>
        </div>
      </div>
    );
  }

  const statusKey = `status${novel.status.charAt(0).toUpperCase()}${novel.status.slice(1)}` as
    | "statusDraft"
    | "statusOngoing"
    | "statusCompleted"
    | "statusPaused";

  const handleDeleteClick = () => {
    if (confirm(tb("deleteConfirm", { title: novel.title }))) {
      onDelete(novel._id);
    }
  };

  const goWriting = () => {
    router.push(`/${locale}/writing/${novel._id}`);
  };

  return (
    <div className="flex h-full min-w-0 flex-col">
      {mobileBack}
      <div className="min-h-0 flex-1 overflow-y-auto">
        <Card className="min-h-full">
        <Card.Header>
          <div className="flex w-full flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
            <div className="flex min-w-0 flex-1 items-start gap-3 sm:gap-4">
              {coverUrl && !(managedCoverId && managedCoverFailed) ? (
                <img
                  src={getImageUrl(coverUrl)}
                  alt={novel.title}
                  className="h-28 w-20 shrink-0 rounded-lg object-cover shadow-sm sm:h-32 sm:w-24"
                  onError={() => {
                    if (managedCoverId) {
                      setFailedManagedCoverId(managedCoverId);
                    }
                  }}
                />
              ) : managedCoverId ? (
                <div
                  role="status"
                  className="flex h-28 w-20 shrink-0 items-center justify-center rounded-lg bg-muted/30 px-2 text-center text-xs text-muted sm:h-32 sm:w-24"
                >
                  {tb("coverAssetMissing")}
                </div>
              ) : (
                <div className="flex h-28 w-20 shrink-0 items-center justify-center rounded-lg bg-muted/30 sm:h-32 sm:w-24">
                  <svg
                    xmlns="http://www.w3.org/2000/svg"
                    width="32"
                    height="32"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.5"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    className="text-muted"
                  >
                    <path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H19a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H6.5a1 1 0 0 1 0-5H20" />
                  </svg>
                </div>
              )}

              <div className="min-w-0 flex-1">
                <h2 className="break-words text-xl font-bold text-foreground">
                  {novel.title}
                </h2>
                {novel.subtitle && (
                  <p className="text-sm text-muted mt-0.5">{novel.subtitle}</p>
                )}
                <div className="flex items-center gap-2 mt-2 flex-wrap">
                  <Chip variant={STATUS_COLOR_MAP[novel.status] || "secondary"} size="sm">
                    {t(statusKey)}
                  </Chip>
                  {novel.genre !== "unclassified" && (
                    <Chip variant="soft" size="sm">
                      {novel.genre}
                    </Chip>
                  )}
                </div>
                {novel.tags && novel.tags.length > 0 && (
                  <div className="flex gap-1 mt-2 flex-wrap">
                    {novel.tags.map((tag) => (
                      <Chip key={tag} variant="tertiary" size="sm">
                        {tag}
                      </Chip>
                    ))}
                  </div>
                )}
              </div>
            </div>

            <div className="flex w-full shrink-0 gap-2 sm:ml-4 sm:w-auto">
              <Button
                variant="primary"
                size="sm"
                onPress={goWriting}
                className="min-h-11 flex-1 bg-accent text-white hover:bg-accent-hover sm:min-h-0 sm:flex-none"
              >
                {tb("goWriting")}
              </Button>
              <Button
                variant="danger-soft"
                size="sm"
                onPress={handleDeleteClick}
                className="min-h-11 flex-1 sm:min-h-0 sm:flex-none"
              >
                {t("delete")}
              </Button>
            </div>
          </div>
        </Card.Header>

        <Card.Content>
          <div className="space-y-4">
            <div className="flex flex-wrap gap-x-4 gap-y-2 text-sm text-muted">
              <span>
                {t("chapterCount")}: {novel.stats?.chapter_count ?? 0}
              </span>
              <span>
                {t("totalWords")}: {(novel.stats?.total_word_count ?? 0).toLocaleString()}
              </span>
              <span>
                {t("createdAt")}: {new Date(novel.created_at).toLocaleDateString()}
              </span>
            </div>

            {PREVIEW_LONG_FIELDS.map(({ key, labelKey }) => {
              const value = novel[key];
              if (!value) return null;
              return (
                <PreviewField
                  key={key}
                  label={t(labelKey)}
                  value={String(value)}
                />
              );
            })}

            {/* writing_style / narrative_pov / era_background — blockquote 3-col */}
            {STYLE_THREE_COLS.some(({ key }) => novel[key]) && (
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
                {STYLE_THREE_COLS.map(({ key, labelKey }) => (
                  <div key={key} className="border-t border-border pt-3">
                    <h3 className="text-xs font-semibold text-muted uppercase tracking-wide mb-1">
                      {t(labelKey)}
                    </h3>
                    <p className="text-sm text-foreground/80 whitespace-pre-wrap leading-relaxed">
                      {novel[key] ? String(novel[key]) : "-"}
                    </p>
                  </div>
                ))}
              </div>
            )}
          </div>
        </Card.Content>
      </Card>
      </div>
    </div>
  );
}

function PreviewField({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <h3 className="text-xs font-semibold text-muted uppercase tracking-wide">
        {label}
      </h3>
      <div className="pt-1 pb-1">
        <p className="text-sm text-foreground/80 whitespace-pre-wrap leading-relaxed">
          {value}
        </p>
      </div>
    </div>
  );
}
