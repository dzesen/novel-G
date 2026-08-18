"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Button, Chip } from "@heroui/react";
import AutoResizeTextarea from "./AutoResizeTextarea";
import BoundedStyleControls from "./BoundedStyleControls";
import CollapsibleField from "./CollapsibleField";
import { ApiError, apiPut, apiPostForm, getImageUrl } from "@/lib/api";
import {
  DANGEROUS_FIELDS,
  FIELD_LABEL_MAP,
  LONG_TEXT_FIELDS,
  SECTION_FIELDS,
  type NovelInfoFieldDef,
  type SectionKey,
} from "@/lib/novelFields";
import { normalizeStyleControls } from "@/lib/styleControls";

export type { SectionKey } from "@/lib/novelFields";

/* component props */

interface NovelInfoSectionProps {
  sectionKey: SectionKey;
  data: Record<string, unknown>;
  novelId?: string;
  isCreateMode: boolean;
  isEditing: boolean;
  onStartEdit: () => void;
  onCancelEdit: () => void;
  onSaved: () => void;
  onChange?: (field: string, value: unknown) => void;
  hasChapters?: boolean;
  onDangerConfirm?: () => Promise<boolean>;
}

export default function NovelInfoSection({
  sectionKey,
  data,
  novelId,
  isCreateMode,
  isEditing,
  onStartEdit,
  onCancelEdit,
  onSaved,
  onChange,
  hasChapters = false,
  onDangerConfirm,
}: NovelInfoSectionProps) {
  const t = useTranslations("novel");
  const tw = useTranslations("writing.novelInfo");
  const fields = SECTION_FIELDS[sectionKey];

  const [editData, setEditData] = useState<Record<string, unknown>>({});
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [tagInput, setTagInput] = useState("");
  const [coverPreviewFailed, setCoverPreviewFailed] = useState(false);
  const [clearManagedCoverOnSave, setClearManagedCoverOnSave] =
    useState(false);

  useEffect(() => {
    setCoverPreviewFailed(false);
  }, [data.cover_asset_id, data.cover_image]);

  const startEdit = () => {
    const snapshot: Record<string, unknown> = {};
    for (const f of fields) {
      snapshot[f.key] = data[f.key] ?? (f.type === "tags" ? [] : f.type === "number" ? 0 : "");
    }
    if (sectionKey === "style") {
      snapshot.style_controls = normalizeStyleControls(data.style_controls);
    }
    setClearManagedCoverOnSave(false);
    setSaveError("");
    setEditData(snapshot);
    onStartEdit();
  };

  const cancelEdit = () => {
    setEditData({});
    setSaveError("");
    setTagInput("");
    setClearManagedCoverOnSave(false);
    onCancelEdit();
  };

  const hasDangerousChanges = (): boolean => {
    if (!hasChapters) return false;
    if (
      sectionKey === "style" &&
      JSON.stringify(normalizeStyleControls(data.style_controls)) !==
        JSON.stringify(normalizeStyleControls(editData.style_controls))
    ) {
      return true;
    }
    return fields.some((f) => {
      if (!DANGEROUS_FIELDS.has(f.key)) return false;
      const orig = String(data[f.key] ?? "");
      const edit = String(editData[f.key] ?? "");
      return orig !== edit;
    });
  };

  const saveSection = async () => {
    if (!isCreateMode && novelId) {
      if (hasDangerousChanges() && onDangerConfirm) {
        const confirmed = await onDangerConfirm();
        if (!confirmed) return;
      }
      try {
        setSaving(true);
        setSaveError("");
        await apiPut(`/api/novels/${novelId}`, editData);
        if (
          sectionKey === "basic" &&
          clearManagedCoverOnSave &&
          data.cover_asset_id
        ) {
          await apiPut(`/api/novels/${novelId}/cover/current`, {
            asset_id: null,
          });
        }
        setClearManagedCoverOnSave(false);
        onSaved();
      } catch (cause) {
        setSaveError(
          sectionKey === "style"
            ? tw("styleControls.validationError")
            : cause instanceof ApiError
              ? cause.message
              : tw("saveFailed"),
        );
      } finally {
        setSaving(false);
      }
      return;
    }
    onCancelEdit();
  };

  const updateField = (key: string, value: unknown) => {
    if (isCreateMode && onChange) {
      onChange(key, value);
    } else {
      setEditData((prev) => ({ ...prev, [key]: value }));
    }
  };

  const getValue = (key: string): unknown => {
    if (isCreateMode) return data[key];
    if (isEditing) return editData[key];
    return data[key];
  };

  /* tag helpers */
  const addTag = (tag: string) => {
    const trimmed = tag.trim();
    if (!trimmed) return;
    const current = (getValue("tags") as string[]) || [];
    if (!current.includes(trimmed)) {
      updateField("tags", [...current, trimmed]);
    }
    setTagInput("");
  };

  const removeTag = (tag: string) => {
    const current = (getValue("tags") as string[]) || [];
    updateField("tags", current.filter((t) => t !== tag));
  };

  /* cover upload */
  const handleCoverUpload = async (file: File) => {
    if (file.size > 2 * 1024 * 1024) return;
    const fd = new FormData();
    fd.append("file", file);
    try {
      const res = await apiPostForm<{ url: string }>("/api/upload/cover", fd);
      updateField("cover_image", res.url);
      setClearManagedCoverOnSave(true);
      setCoverPreviewFailed(false);
    } catch {
      // silently handle
    }
  };

  /* render helpers */

  const renderReadField = (f: NovelInfoFieldDef) => {
    const val = data[f.key];
    const label = t(FIELD_LABEL_MAP[f.key] || f.key);

    if (f.type === "cover") {
      const assetId = data.cover_asset_id
        ? String(data.cover_asset_id)
        : "";
      const url = assetId
        ? `/api/image-assets/${encodeURIComponent(assetId)}/content`
        : val as string | undefined;
      const missing = Boolean(assetId && coverPreviewFailed);
      return (
        <div key={f.key} className="flex items-center gap-3">
          <span className="text-sm text-muted w-20 shrink-0">{label}</span>
          {url && !missing ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={getImageUrl(url)}
              alt={t("coverPreviewAlt")}
              className="w-16 h-20 rounded object-cover"
              onError={() => setCoverPreviewFailed(true)}
            />
          ) : missing ? (
            <span
              role="status"
              className="flex h-20 w-16 items-center justify-center rounded border border-border bg-surface-secondary px-1 text-center text-xs text-muted"
            >
              {t("coverAssetMissing")}
            </span>
          ) : (
            <span className="text-sm text-muted/50">—</span>
          )}
        </div>
      );
    }

    if (f.type === "tags") {
      const tags = (val as string[]) || [];
      return (
        <div key={f.key} className="flex items-start gap-3">
          <span className="text-sm text-muted w-20 shrink-0 pt-0.5">{label}</span>
          <div className="flex flex-wrap gap-1.5">
            {tags.length > 0
              ? tags.map((tag) => (
                  <Chip key={tag} variant="soft" size="sm">{tag}</Chip>
                ))
              : <span className="text-sm text-muted/50">—</span>}
          </div>
        </div>
      );
    }

    if (LONG_TEXT_FIELDS.has(f.key)) {
      return (
        <CollapsibleField
          key={f.key}
          label={label}
          value={String(val ?? "")}
          noContentText={tw("noContent")}
        />
      );
    }

    if (f.type === "select" && f.options) {
      const display = f.options.find((o) => o.value === val);
      return (
        <div key={f.key} className="flex items-center gap-3">
          <span className="text-sm text-muted w-20 shrink-0">{label}</span>
          <span className="text-sm text-foreground">
            {display ? t(display.labelKey) : String(val ?? "—")}
          </span>
        </div>
      );
    }

    return (
      <div key={f.key} className="flex items-center gap-3">
        <span className="text-sm text-muted w-20 shrink-0">{label}</span>
        <span className="text-sm text-foreground">{val != null && val !== "" ? String(val) : "—"}</span>
      </div>
    );
  };

  const renderEditField = (f: NovelInfoFieldDef) => {
    const val = getValue(f.key);
    const label = t(FIELD_LABEL_MAP[f.key] || f.key);

    if (f.type === "cover") {
      const assetId =
        !clearManagedCoverOnSave && data.cover_asset_id
          ? String(data.cover_asset_id)
          : "";
      const url = assetId
        ? `/api/image-assets/${encodeURIComponent(assetId)}/content`
        : val as string | undefined;
      const missing = Boolean(assetId && coverPreviewFailed);
      return (
        <div key={f.key} className="space-y-2">
          <label className="text-sm font-medium text-foreground">{label}</label>
          <div className="flex items-center gap-3">
            {url && !missing ? (
              <>
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={getImageUrl(url)}
                  alt={t("coverPreviewAlt")}
                  className="w-16 h-20 rounded object-cover"
                  onError={() => setCoverPreviewFailed(true)}
                />
                <Button
                  variant="ghost"
                  size="sm"
                  onPress={() => {
                    updateField("cover_image", "");
                    setClearManagedCoverOnSave(true);
                    setCoverPreviewFailed(false);
                  }}
                >
                  {t("removeCover")}
                </Button>
              </>
            ) : missing ? (
              <>
                <span
                  role="status"
                  className="flex h-20 w-16 items-center justify-center rounded border border-border bg-surface-secondary px-1 text-center text-xs text-muted"
                >
                  {t("coverAssetMissing")}
                </span>
                <Button
                  variant="ghost"
                  size="sm"
                  onPress={() => {
                    updateField("cover_image", "");
                    setClearManagedCoverOnSave(true);
                    setCoverPreviewFailed(false);
                  }}
                >
                  {t("removeCover")}
                </Button>
              </>
            ) : null}
            <label className="cursor-pointer text-sm text-accent hover:underline">
              {t("uploadCover")}
              <input
                type="file"
                accept="image/*"
                className="hidden"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) handleCoverUpload(file);
                }}
              />
            </label>
          </div>
        </div>
      );
    }

    if (f.type === "tags") {
      const tags = (val as string[]) || [];
      return (
        <div key={f.key} className="space-y-2">
          <label className="text-sm font-medium text-foreground">{label}</label>
          <div className="flex flex-wrap gap-1.5 mb-2">
            {tags.map((tag) => (
              <Chip key={tag} variant="soft" size="sm">
                {tag}
                <button
                  type="button"
                  className="ml-1 text-muted hover:text-foreground"
                  onClick={() => removeTag(tag)}
                >
                  ×
                </button>
              </Chip>
            ))}
          </div>
          <input
            className="w-full rounded-lg border border-border bg-background px-3 py-1.5 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
            value={tagInput}
            onChange={(e) => setTagInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                addTag(tagInput);
              }
            }}
            placeholder={t("tags")}
          />
        </div>
      );
    }

    if (f.type === "textarea") {
      return (
        <div key={f.key} className="space-y-2">
          <label className="text-sm font-medium text-foreground">{label}</label>
          <AutoResizeTextarea
            value={String(val ?? "")}
            onChange={(v) => updateField(f.key, v)}
            placeholder={label}
          />
        </div>
      );
    }

    if (f.type === "number") {
      return (
        <div key={f.key} className="space-y-2">
          <label className="text-sm font-medium text-foreground">{label}</label>
          <input
            type="number"
            className="w-full rounded-lg border border-border bg-background px-3 py-1.5 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
            value={val != null ? Number(val) : ""}
            onChange={(e) => updateField(f.key, e.target.value ? Number(e.target.value) : undefined)}
            min={f.min}
            max={f.max}
          />
        </div>
      );
    }

    if (f.type === "select" && f.options) {
      return (
        <div key={f.key} className="space-y-2">
          <label className="text-sm font-medium text-foreground">{label}</label>
          <select
            className="w-full rounded-lg border border-border bg-background px-3 py-1.5 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
            value={String(val ?? "")}
            onChange={(e) => updateField(f.key, e.target.value)}
          >
            <option value="">—</option>
            {f.options.map((o) => (
              <option key={o.value} value={o.value}>{t(o.labelKey)}</option>
            ))}
          </select>
        </div>
      );
    }

    return (
      <div key={f.key} className="space-y-2">
        <label className="text-sm font-medium text-foreground">{label}</label>
        <input
          className="w-full rounded-lg border border-border bg-background px-3 py-1.5 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
          value={String(val ?? "")}
          onChange={(e) => updateField(f.key, e.target.value)}
          placeholder={label}
        />
      </div>
    );
  };

  /* main render */

  const sectionTitle = tw(`sections.${sectionKey}`);
  const showEditButton = !isCreateMode && !isEditing;
  const showSaveCancel = !isCreateMode && isEditing;

  return (
    <div className="border border-border rounded-xl bg-surface">
      {/* section header */}
      <div className="flex items-center justify-between px-5 py-3 border-b border-border/50">
        <h3 className="text-sm font-semibold text-foreground">{sectionTitle}</h3>
        {showEditButton && (
          <Button variant="ghost" size="sm" onPress={startEdit}>
            {tw("editSection")}
          </Button>
        )}
        {showSaveCancel && (
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" onPress={cancelEdit} isDisabled={saving}>
              {tw("cancelEdit")}
            </Button>
            <Button
              variant="primary"
              size="sm"
              onPress={saveSection}
              isDisabled={saving}
              className="bg-accent text-white hover:bg-accent-hover"
            >
              {saving ? tw("saving") : tw("saveSection")}
            </Button>
          </div>
        )}
      </div>

      {/* section body */}
      <div className="px-5 py-4 space-y-3">
        {(isEditing || isCreateMode)
          ? fields.map(renderEditField)
          : fields.map(renderReadField)}
        {sectionKey === "style" ? (
          <BoundedStyleControls
            value={normalizeStyleControls(getValue("style_controls"))}
            isEditing={isEditing || isCreateMode}
            onChange={(value) => updateField("style_controls", value)}
          />
        ) : null}
        {saveError ? (
          <p role="alert" className="text-sm text-red-600 dark:text-red-400">
            {saveError}
          </p>
        ) : null}
      </div>
    </div>
  );
}
