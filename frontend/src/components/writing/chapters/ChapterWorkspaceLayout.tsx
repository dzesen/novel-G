"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useState,
  type ReactNode,
} from "react";
import { useTranslations } from "next-intl";
import { Drawer } from "@/components/ui/Drawer";
import {
  initialChapterWorkspaceLayoutState,
  reduceChapterWorkspaceLayout,
  type ChapterWorkspaceDrawer,
} from "./chapterWorkspaceLayoutState";

export interface ChapterWorkspaceLayoutControls {
  directoryVisible: boolean;
  contextVisible: boolean;
  openDirectory: () => void;
  openContext: () => void;
  openAssistant: () => void;
  toggleDirectory: () => void;
  toggleContext: () => void;
  closeDrawer: () => void;
}

interface ChapterWorkspaceLayoutProps {
  renderDirectory: (controls: ChapterWorkspaceLayoutControls) => ReactNode;
  renderEditor: (controls: ChapterWorkspaceLayoutControls) => ReactNode;
  renderContext: (controls: ChapterWorkspaceLayoutControls) => ReactNode;
  renderAssistant: (controls: ChapterWorkspaceLayoutControls) => ReactNode;
}

function useWorkspaceBreakpoints() {
  const [breakpoints, setBreakpoints] = useState({
    directoryRail: false,
    contextRail: false,
  });
  useEffect(() => {
    const directoryQuery = window.matchMedia("(min-width: 768px)");
    const contextQuery = window.matchMedia("(min-width: 1280px)");
    const sync = () => setBreakpoints({
      directoryRail: directoryQuery.matches,
      contextRail: contextQuery.matches,
    });
    sync();
    directoryQuery.addEventListener("change", sync);
    contextQuery.addEventListener("change", sync);
    return () => {
      directoryQuery.removeEventListener("change", sync);
      contextQuery.removeEventListener("change", sync);
    };
  }, []);
  return breakpoints;
}

export default function ChapterWorkspaceLayout({
  renderDirectory,
  renderEditor,
  renderContext,
  renderAssistant,
}: ChapterWorkspaceLayoutProps) {
  const t = useTranslations("writing.chapterEditor.workspace");
  const [state, dispatch] = useReducer(
    reduceChapterWorkspaceLayout,
    initialChapterWorkspaceLayoutState,
  );
  const { directoryRail, contextRail } = useWorkspaceBreakpoints();
  const closeDrawer = useCallback(
    () => dispatch({ type: "close-drawer" }),
    [],
  );
  const openDrawer = useCallback((drawer: ChapterWorkspaceDrawer) => {
    dispatch({ type: "open-drawer", drawer });
  }, []);

  useEffect(() => {
    if (
      (state.drawer === "directory" && directoryRail)
      || (state.drawer === "context" && contextRail)
    ) {
      closeDrawer();
    }
  }, [closeDrawer, contextRail, directoryRail, state.drawer]);

  const controls = useMemo<ChapterWorkspaceLayoutControls>(
    () => ({
      directoryVisible: state.directoryVisible,
      contextVisible: state.contextVisible,
      openDirectory: () => openDrawer("directory"),
      openContext: () => openDrawer("context"),
      openAssistant: () => openDrawer("assistant"),
      toggleDirectory: () => dispatch({ type: "toggle-directory" }),
      toggleContext: () => dispatch({ type: "toggle-context" }),
      closeDrawer,
    }),
    [closeDrawer, openDrawer, state.contextVisible, state.directoryVisible],
  );

  return (
    <div className="flex h-full min-h-0 min-w-0 bg-background">
      {directoryRail && state.directoryVisible && (
        <aside
          aria-label={t("directory")}
          className="w-72 shrink-0 border-r border-border bg-surface-secondary/45"
        >
          {renderDirectory(controls)}
        </aside>
      )}

      <section className="min-h-0 min-w-0 flex-1">
        {renderEditor(controls)}
      </section>

      {contextRail && state.contextVisible && (
        <aside
          aria-label={t("context")}
          className="w-80 shrink-0 border-l border-border bg-surface"
        >
          {renderContext(controls)}
        </aside>
      )}

      {!directoryRail && (
          <Drawer
            open={state.drawer === "directory"}
            onClose={closeDrawer}
            title={t("directory")}
            description={t("directoryDescription")}
            closeLabel={t("close")}
            side="left"
            panelClassName="max-w-[22rem]"
          >
            {renderDirectory(controls)}
          </Drawer>
      )}
      {!contextRail && (
          <Drawer
            open={state.drawer === "context"}
            onClose={closeDrawer}
            title={t("context")}
            description={t("contextDescription")}
            closeLabel={t("close")}
          >
            {renderContext(controls)}
          </Drawer>
      )}

      <Drawer
        open={state.drawer === "assistant"}
        onClose={closeDrawer}
        title={t("assistant")}
        description={t("assistantDescription")}
        closeLabel={t("close")}
      >
        {renderAssistant(controls)}
      </Drawer>
    </div>
  );
}
