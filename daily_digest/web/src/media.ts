import type { Article, Edition } from './types';
import entries from './assets/news-media.json';

const files = import.meta.glob('./assets/*.{png,jpg,jpeg,webp,svg}', {
  eager: true,
  query: '?url',
  import: 'default',
}) as Record<string, string>;
export type NewsImage = {
  file: string;
  source_url: string;
  caption: string;
  credit: string;
  illustration?: boolean;
  kind?: string;
  width?: number;
  height?: number;
};

export function fallbackImage(article: Article, edition: Edition) {
  const key = article.source_ids
    .map((id) => edition.sources.find((s) => s.id === id)?.image?.fallback_key)
    .find(Boolean);
  const society = edition.articles.society.some((item) => item.id === article.id);
  return {
    src: files[
      './assets/' +
        (key === 'landscape'
          ? 'city-engraving.webp'
          : key === 'society-city' || (!key && society)
            ? 'mainland-hongkong-v2.webp'
            : 'technology.svg')
    ],
    illustration: true,
    kind: 'illustration',
    caption: '分类主题插画，非新闻现场',
    credit: '本地分类插画',
    width: undefined,
    height: undefined,
  };
}

export function articleImage(article: Article, edition: Edition) {
  const sources = article.source_ids
    .map((id) => edition.sources.find((source) => source.id === id))
    .filter((source) => source !== undefined);
  const bound =
    sources.find((item) => item.image?.asset_id) ||
    sources.find((item) => item.image?.kind === 'illustration');
  if (bound?.image?.asset_id)
    return { ...bound.image, src: '/media/' + bound.image.asset_id, illustration: false };
  if (bound?.image?.kind === 'illustration') return fallbackImage(article, edition);
  const urls = edition.sources
    .filter((source) => article.source_ids.includes(source.id))
    .map((source) => source.url);
  const entry = (entries as NewsImage[]).find((item) => urls.includes(item.source_url));
  const src = entry && files['./assets/' + entry.file];
  if (src && entry) return { ...entry, src };
  return fallbackImage(article, edition);
}

export function headlineArticle(edition: Edition) {
  const articles = edition.articles.ai;
  return articles.find((article) => !articleImage(article, edition).illustration) || articles[0];
}

export function societyArticles(edition: Edition, unavailable: string[] = []) {
  const articles = edition.articles.society;
  const lead = articles.find((article) => {
    const image = articleImage(article, edition);
    return image && !image.illustration && !unavailable.includes(article.id);
  });
  return lead ? [lead, ...articles.filter((article) => article.id !== lead.id)] : articles;
}
