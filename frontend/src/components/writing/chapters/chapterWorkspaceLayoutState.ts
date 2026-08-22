export type ChapterWorkspaceDrawer = "directory" | "context" | "assistant";

export interface ChapterWorkspaceLayoutState {
  directoryVisible: boolean;
  contextVisible: boolean;
  drawer: ChapterWorkspaceDrawer | null;
}

export type ChapterWorkspaceLayoutAction =
  | { type: "toggle-directory" }
  | { type: "toggle-context" }
  | { type: "open-drawer"; drawer: ChapterWorkspaceDrawer }
  | { type: "close-drawer" };

export const initialChapterWorkspaceLayoutState: ChapterWorkspaceLayoutState = {
  directoryVisible: true,
  contextVisible: true,
  drawer: null,
};

export function reduceChapterWorkspaceLayout(
  state: ChapterWorkspaceLayoutState,
  action: ChapterWorkspaceLayoutAction,
): ChapterWorkspaceLayoutState {
  if (action.type === "toggle-directory") {
    return { ...state, directoryVisible: !state.directoryVisible };
  }
  if (action.type === "toggle-context") {
    return { ...state, contextVisible: !state.contextVisible };
  }
  if (action.type === "open-drawer") {
    return { ...state, drawer: action.drawer };
  }
  return { ...state, drawer: null };
}
