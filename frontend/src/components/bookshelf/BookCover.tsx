/* eslint-disable @next/next/no-img-element */
"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { getImageUrl } from "@/lib/api";
import type { NovelSummary } from "@/types/novel";

/** Managed assets remain authoritative, including when their content is missing. */
export default function BookCover({ novel, className = "" }: { novel: NovelSummary; className?: string }) {
  const t = useTranslations("bookshelf");
  const managedId = novel.cover_asset_id ? String(novel.cover_asset_id) : "";
  const source = managedId ? `/api/image-assets/${encodeURIComponent(managedId)}/content` : novel.cover_image;
  const [failedSource, setFailedSource] = useState<string | null>(null);
  const color = Array.from(novel._id).reduce((sum, char) => sum + char.charCodeAt(0), 0) % 4;
  return (
    <div className={`book-cover book-cover-tone-${color} ${className}`}>
      {source && failedSource !== source ? (
        <img src={getImageUrl(source)} alt={novel.title} onError={() => setFailedSource(source)} />
      ) : (
        <div className="book-cover-type">
          <span className="book-cover-rule" aria-hidden="true" />
          <span className="book-cover-title">{novel.title}</span>
          <span className="book-cover-caption">{managedId ? <span role="status">{t("coverAssetMissing")}</span> : novel.genre !== "unclassified" ? novel.genre : t("manuscript")}</span>
        </div>
      )}
    </div>
  );
}
