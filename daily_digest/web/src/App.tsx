import { useEffect, useState } from 'react';
import type { Edition } from './types';
import { demo } from './demo';
import { parseRoute } from './routes';
import { Header, Footer } from './components/PageLayout';
import { FrontPage } from './pages/FrontPage';
import { ArticlePage, AnalysisPage, MarketsPage, NotesPage } from './pages/EditionPages';
import { Archive } from './pages/Archive';
import { Empty } from './pages/Empty';

export function App() {
  const route = parseRoute(window.location.pathname);
  const [edition, setEdition] = useState<Edition | null>(route.sample ? demo : null);
  const [state, setState] = useState<'loading' | 'ready' | 'empty' | 'error'>(
    route.sample ? 'ready' : 'loading',
  );
  useEffect(() => {
    if (!edition) return;
    const article =
      route.kind === 'article'
        ? [...edition.articles.ai, ...edition.articles.society].find((item) => item.id === route.id)
        : null;
    const analysis =
      route.kind === 'analysis' && /^\d+$/.test(route.id || '')
        ? edition.analysis[Number(route.id)]
        : null;
    const title =
      article?.title ||
      analysis?.title ||
      (route.kind === 'markets'
        ? '两地收盘观察'
        : route.kind === 'notes'
          ? '本期编辑说明与来源'
          : edition.title);
    document.title = title + ' · 每日观察';
  }, [edition, route.kind, route.id]);
  useEffect(() => {
    if (route.sample || route.kind === 'archive' || route.kind === 'missing') return;
    let active = true;
    fetch('/api/editions/' + (route.date || 'latest'))
      .then(async (response) => {
        if (response.status === 404) {
          if (active) setState('empty');
          return;
        }
        if (!response.ok) throw Error();
        const data = await response.json();
        if (
          !data.date ||
          !data.articles?.ai?.length ||
          !data.articles?.society?.length ||
          !data.markets ||
          !Array.isArray(data.sources)
        )
          throw Error();
        if (active) {
          setEdition(data);
          setState('ready');
        }
      })
      .catch(() => {
        if (active) setState('error');
      });
    return () => {
      active = false;
    };
  }, [route.date, route.sample, route.kind]);
  if (route.kind === 'archive') return <Archive />;
  if (route.kind === 'missing') return <Empty missing />;
  if (state === 'loading')
    return (
      <>
        <Header />
        <main className="empty" role="status">
          <div className="kicker">PLEASE WAIT</div>
          <h1>正在展开今天的报纸…</h1>
        </main>
        <Footer />
      </>
    );
  if (state !== 'ready' || !edition)
    return <Empty error={state === 'error'} missing={state === 'empty' && Boolean(route.date)} />;
  if (route.kind === 'article') return <ArticlePage edition={edition} id={route.id!} />;
  if (route.kind === 'analysis') return <AnalysisPage edition={edition} id={route.id!} />;
  if (route.kind === 'markets') return <MarketsPage edition={edition} />;
  if (route.kind === 'notes') return <NotesPage edition={edition} />;
  return <FrontPage edition={edition} />;
}
