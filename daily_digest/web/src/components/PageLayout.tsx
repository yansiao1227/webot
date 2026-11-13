import type { ReactNode } from 'react';
import type { Edition } from '../types';
import { editionBase } from '../routes';
import { time, pretty } from '../utils/format';
import { EditionNote } from './EditionContent';

export function Header({ edition }: { edition?: Edition }) {
  const base = edition ? editionBase(edition.date, edition.simulation) : '/';
  return (
    <>
      <div className="topline">
        <span>新闻有出处 · 观察有背景</span>
        <span>独立整理 / 每日一版</span>
      </div>
      <header className="masthead">
        <div className="masthead-side">
          <span>读懂今天</span>
          <small>看见新闻之间的联系</small>
        </div>
        <a className="wordmark" href={base}>
          每日观察<span>THE DAILY OBSERVER</span>
        </a>
        <div className="edition-date">
          {edition ? pretty(edition.date) : '中文独立日报'}
          <small>
            {edition ? '内容截止 ' + time(edition.cutoff_at) + ' · 北京时间' : 'AI · 社会 · 市场'}
          </small>
        </div>
      </header>
    </>
  );
}
export function Footer({ edition }: { edition?: Edition }) {
  if (edition) {
    return (
      <footer className="edition-footer">
        <a className="footer-brand" href="/">
          每日观察<small>THE DAILY OBSERVER</small>
        </a>
        <div className="edition-footer-links">
          <a href={editionBase(edition.date, edition.simulation) + '/notes'}>阅读说明与来源</a>
          <a href="/archive">翻阅往期</a>
        </div>
        <p className="edition-footer-meta">
          本期 {edition.articles.ai.length + edition.articles.society.length} 篇报道 ·{' '}
          {edition.sources.length} 个来源 · 公开资料整理
        </p>
      </footer>
    );
  }
  return (
    <footer>
      <a className="footer-brand" href="/">
        每日观察<small>THE DAILY OBSERVER</small>
      </a>
      <p>
        公开来源整理 · 事实与分析分开呈现
        <br />
        每一条新闻，都有可追溯的出处。
      </p>
      <a href="/archive">翻阅往期</a>
    </footer>
  );
}

export function DetailFrame({
  edition,
  children,
  label,
}: {
  edition: Edition;
  children: ReactNode;
  label: string;
}) {
  const base = editionBase(edition.date, edition.simulation);
  return (
    <>
      <EditionNote edition={edition} />
      <Header edition={edition} />
      <main className="detail-page">
        <div className="breadcrumb">
          <a href={base}>返回当期头版</a>
          <span>
            {label} / {edition.date}
          </span>
        </div>
        {children}
        <div className="back-bottom">
          <a href={base}>回到头版，继续阅读其他报道</a>
        </div>
      </main>
      <Footer />
    </>
  );
}
