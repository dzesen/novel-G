const CJK_OR_WORD =
  /[\u3400-\u4dbf\u4e00-\u9fff]|[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*/g;

/** Matches backend count_chapter_words: one CJK codepoint or one Latin/number word. */
export function countChapterWords(content: string): number {
  return content.match(CJK_OR_WORD)?.length ?? 0;
}
