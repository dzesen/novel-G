export interface ReferenceCardListItem {
  _id: string;
  name: string;
  subtitle: string;
  description: string;
  tags: readonly string[];
  character_profile?: {
    aliases?: readonly string[];
  };
}

interface ReferenceCardListOptions {
  search: string;
  favoriteCardIds: ReadonlySet<string>;
  favoritesOnly: boolean;
  favoritesFirst: boolean;
}

export function organizeReferenceCards<T extends ReferenceCardListItem>(
  cards: readonly T[],
  {
    search,
    favoriteCardIds,
    favoritesOnly,
    favoritesFirst,
  }: ReferenceCardListOptions,
): T[] {
  const needle = search.trim().toLocaleLowerCase();
  const filtered = cards.filter((card) => {
    if (favoritesOnly && !favoriteCardIds.has(card._id)) return false;
    if (!needle) return true;
    return [
      card.name,
      card.subtitle,
      card.description,
      ...card.tags,
      ...(card.character_profile?.aliases ?? []),
    ]
      .join(" ")
      .toLocaleLowerCase()
      .includes(needle);
  });

  if (!favoritesFirst) return filtered;
  return filtered
    .map((card, index) => ({ card, index }))
    .sort((left, right) => {
      const favoriteDifference =
        Number(favoriteCardIds.has(right.card._id)) -
        Number(favoriteCardIds.has(left.card._id));
      return favoriteDifference || left.index - right.index;
    })
    .map(({ card }) => card);
}
