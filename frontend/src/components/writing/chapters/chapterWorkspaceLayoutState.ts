export type ChapterWorkspaceDrawer = "directory" | "context" | "assistant";
export type ChapterWorkspaceTool = "assistant" | "context";

export interface ChapterWorkspaceLayoutState {
  directoryVisible: boolean;
  toolsVisible: boolean;
  activeTool: ChapterWorkspaceTool;
  drawer: ChapterWorkspaceDrawer | null;
}

export type ChapterWorkspaceLayoutAction =
  | { type: "toggle-directory" }
  | { type: "show-tool"; tool: ChapterWorkspaceTool }
  | { type: "hide-tools" }
  | { type: "open-drawer"; drawer: ChapterWorkspaceDrawer }
  | { type: "close-drawer" };

export const initialChapterWorkspaceLayoutState: ChapterWorkspaceLayoutState = {
  directoryVisible: true,
  toolsVisible: true,
  activeTool: "assistant",
  drawer: null,
};

export function reduceChapterWorkspaceLayout(
  state: ChapterWorkspaceLayoutState,
  action: ChapterWorkspaceLayoutAction,
): ChapterWorkspaceLayoutState {
  if (action.type === "toggle-directory") {
    return { ...state, directoryVisible: !state.directoryVisible };
  }
  if (action.type === "show-tool") {
    return { ...state, toolsVisible: true, activeTool: action.tool, drawer: null };
  }
  if (action.type === "hide-tools") {
    return { ...state, toolsVisible: false };
  }
  if (action.type === "open-drawer") {
    return { ...state, drawer: action.drawer };
  }
  return { ...state, drawer: null };
}
