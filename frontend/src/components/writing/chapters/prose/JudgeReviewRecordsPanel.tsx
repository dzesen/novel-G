"use client";

import { useTranslations } from "next-intl";
import { Drawer } from "@/components/ui/Drawer";
import JudgeReviewRecords from "./JudgeReviewRecords";

export default function JudgeReviewRecordsPanel({
  chapterId,
  chapterTitle,
  onClose,
}: {
  chapterId: string;
  chapterTitle: string;
  onClose: () => void;
}) {
  const t = useTranslations("writing.judgeReviews");

  return (
    <Drawer open onClose={onClose} title={t("title")} description={chapterTitle} closeLabel={t("close")} panelClassName="judge-review-drawer">
      <div className="px-5 py-5 sm:px-8 sm:py-6">
        <JudgeReviewRecords chapterId={chapterId} collapsible={false} />
      </div>
    </Drawer>
  );
}
