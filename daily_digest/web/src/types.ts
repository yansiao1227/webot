export type Source = {
  id: string;
  title: string;
  url: string;
  published_at: string;
  captured_at: string;
  image?: {
    url: string;
    caption: string;
    credit: string;
    kind?: 'original' | 'event' | 'theme' | 'illustration';
    page_url?: string;
    asset_id?: string;
    fallback_key?: string;
    width?: number;
    height?: number;
  };
};
export type Article = {
  id: string;
  title: string;
  summary: string;
  background: string;
  impact: string;
  uncertainty: string;
  source_ids: string[];
};
export type Quote = {
  code: string;
  name: string;
  close: number;
  change_pct: number;
  turnover?: number;
};
export type Sector = { code: string; name: string; change_pct: number };
export type Market = {
  status: 'open' | 'closed' | 'unavailable';
  trade_date: string | null;
  as_of: string | null;
  source: string;
  source_url: string;
  classification: string;
  indices: Quote[];
  gainers: Sector[];
  losers: Sector[];
  missing: string[];
};
export type Edition = {
  schema_version: 1;
  date: string;
  cutoff_at: string;
  title: string;
  intro: string;
  teaser: string;
  highlights: string[];
  articles: { ai: Article[]; society: Article[] };
  analysis: { title: string; text: string; source_ids: string[] }[];
  sources: Source[];
  markets: { a: Market; hk: Market };
  warnings: string[];
  simulation: boolean;
  content_hash: string;
};
export type Listing = { date: string; title: string; cutoff_at: string; content_hash: string };
