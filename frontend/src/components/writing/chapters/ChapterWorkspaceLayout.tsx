"use client";

import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useReducer,
  useState,
  type ReactNode,
} from "react";
import { useTranslations } from "next-intl";
import { Drawer } from "@/components/ui/Drawer";
import { IconButton } from "@/components/ui/IconButton";
import {
  initialChapterWorkspaceLayoutState,
  reduceChapterWorkspaceLayout,
  type ChapterWorkspaceDrawer,
} from "./chapterWorkspaceLayoutState";

export interface ChapterWorkspaceLayoutControls {
  focusMode: boolean;
  toggleFocus: () => void;
  directoryVisible: boolean;
  contextVisible: boolean;
  assistantVisible: boolean;
  assistantTriggerId: string;
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
  const [focusMode, setFocusMode] = useState(false);
  const assistantTriggerId = useId();
  const closeDrawer = useCallback(
    () => dispatch({ type: "close-drawer" }),
    [],
  );
  const openDrawer = useCallback((drawer: ChapterWorkspaceDrawer) => {
    dispatch({ type: "open-drawer", drawer });
  }, []);

  useEffect(() => {
    if (contextRail && !focusMode && (state.drawer === "context" || state.drawer === "assistant")) {
      dispatch({ type: "show-tool", tool: state.drawer });
    } else if (state.drawer === "directory" && directoryRail) {
      closeDrawer();
    }
  }, [closeDrawer, contextRail, directoryRail, focusMode, state.drawer]);

  const controls = useMemo<ChapterWorkspaceLayoutControls>(
    () => ({
      focusMode,
      toggleFocus: () => setFocusMode((value) => !value),
      directoryVisible: state.directoryVisible,
      contextVisible: state.toolsVisible && state.activeTool === "context",
      assistantVisible: state.drawer === "assistant" || (contextRail && !focusMode && state.toolsVisible && state.activeTool === "assistant"),
      assistantTriggerId,
      openDirectory: () => openDrawer("directory"),
      openContext: () => openDrawer("context"),
      openAssistant: () => {
        if (contextRail && !focusMode) dispatch({ type: "show-tool", tool: "assistant" });
        else openDrawer("assistant");
      },
      toggleDirectory: () => dispatch({ type: "toggle-directory" }),
      toggleContext: () => dispatch(state.toolsVisible && state.activeTool === "context"
        ? { type: "hide-tools" }
        : { type: "show-tool", tool: "context" }),
      closeDrawer,
    }),
    [assistantTriggerId, closeDrawer, contextRail, focusMode, openDrawer, state.activeTool, state.drawer, state.toolsVisible, state.directoryVisible],
  );

  return (
    <div className="chapter-studio flex h-full min-h-0 min-w-0 bg-background" data-focus={focusMode}>
      {directoryRail && state.directoryVisible && !focusMode && (
        <aside
          aria-label={t("directory")}
          className="studio-directory-rail w-64 shrink-0 border-r border-border bg-surface-secondary/45"
        >
          {renderDirectory(controls)}
        </aside>
      )}

      <section className="min-h-0 min-w-0 flex-1">
        {renderEditor(controls)}
      </section>

      {contextRail && state.toolsVisible && !focusMode && (
        <aside
          aria-label={t(state.activeTool)}
          className="studio-assistant-rail shrink-0 border-l border-border bg-background"
        >
          <div className="studio-tools-header">
            <div className="studio-tools-switch" role="group" aria-label={t("toolsLabel")}>
              {(["assistant", "context"] as const).map((tool) => (
                <button key={tool} type="button" aria-pressed={state.activeTool === tool} onClick={() => dispatch({ type: "show-tool", tool })}>
                  {t(tool)}
                </button>
              ))}
            </div>
            <IconButton size="sm" label={t(state.activeTool === "assistant" ? "hideAssistant" : "hideContext")} onClick={() => {
              dispatch({ type: "hide-tools" });
              document.getElementById(assistantTriggerId)?.focus();
            }}>
              <svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round"><path d="m9 5 7 7-7 7" /></svg>
            </IconButton>
          </div>
          <div className="studio-tools-body">
            {state.activeTool === "assistant" ? renderAssistant(controls) : renderContext(controls)}
          </div>
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
