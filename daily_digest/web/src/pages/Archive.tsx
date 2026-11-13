import { useEffect, useState } from 'react';
import { UnstyledButton } from '@mantine/core';
import type { Listing } from '../types';
import { editionBase } from '../routes';
import { Header, Footer } from '../components/PageLayout';

export function Archive() {
  const [items, setItems] = useState<Listing[]>([]),
    [cursor, setCursor] = useState<string | null>(null),
    [loading, setLoading] = useState(true),
    [error, setError] = useState(false);
  function load(next?: string) {
    return fetch('/api/editions?limit=20' + (next ? '&cursor=' + encodeURIComponent(next) : ''))
      .then(async (response) => {
        if (!response.ok) throw Error();
        const data = await response.json();
        if (!Array.isArray(data.items)) throw Error();
        setItems((old) => (next ? [...old, ...data.items] : data.items));
        setCursor(data.next_cursor);
      })
      .catch(() => setError(true))
      .finally(() => setLoading(false));
  }
  function reload(next?: string) {
    setLoading(true);
    setError(false);
    void load(next);
  }
  useEffect(() => {
    void load();
  }, []);
  return (
    <>
      <Header />
      <main className="archive">
        <div className="kicker">THE ARCHIVE</div>
        <h1 className="page-title">每一天，都值得被认真记录。</h1>
        <p className="page-deck">翻阅往期，也回看当时我们如何理解新闻。</p>
        {items.map((item) => (
          <a className="archive-item" href={editionBase(item.date)} key={item.date}>
            <time>{item.date}</time>
            <h2>{item.title}</h2>
            <span>翻开这一期</span>
          </a>
        ))}
        {!loading && !error && !items.length && <p className="notice">还没有正式生成的日报。</p>}
        {error && (
          <p className="notice">
            暂时无法读取归档。
            <UnstyledButton onClick={() => reload(cursor || undefined)}>重试</UnstyledButton>
          </p>
        )}
        {loading && <p role="status">正在翻阅往期…</p>}
        {cursor && !loading && !error && (
          <UnstyledButton className="outline-link" onClick={() => reload(cursor)}>
            查看更多
          </UnstyledButton>
        )}
      </main>
      <Footer />
    </>
  );
}
