import type { Article, Edition } from '../types';
import { useState } from 'react';
import cityIllustration from '../assets/mainland-hongkong-v2.webp';
import { articleHref, brief, editionBase } from '../routes';
import { articleImage, fallbackImage } from '../media';
import { time, today } from '../utils/format';
import { frontTitle } from '../utils/headline';

export function SectionTitle({
  title,
  english,
  link,
}: {
  title: string;
  english: string;
  link?: string;
}) {
  return (
    <div className="section-title">
      <h2>{title}</h2>
      <span>{english}</span>
      {link && <a href={link}>阅读全文</a>}
    </div>
  );
}
export function EditionNote({ edition }: { edition: Edition }) {
  return (
    <>
      {edition.simulation && (
        <div className="preview-banner">
          排版样本 · 新闻与行情均为模拟内容 <a href="/">返回正式日报</a>
        </div>
      )}
      {!edition.simulation && edition.date !== today() && (
        <div className="notice">正在阅读 {edition.date} 日报，今日日报尚未更新。</div>
      )}
    </>
  );
}

export function Photo({
  article,
  edition,
  large = false,
  fallback = true,
}: {
  article: Article;
  edition: Edition;
  large?: boolean;
  fallback?: boolean;
}) {
  const [failed, setFailed] = useState(false);
  const image = failed ? fallbackImage(article, edition) : articleImage(article, edition);
  if (!fallback && (failed || image.illustration)) return null;
  return (
    <figure className={'news-photo ' + (large ? 'lead-photo' : '')}>
      <img
        src={image.src}
        alt={(image.illustration ? '主题插画 · ' : '') + image.caption}
        width={image.width}
        height={image.height}
        loading={large ? 'eager' : 'lazy'}
        referrerPolicy="no-referrer"
        onError={image.illustration ? undefined : () => setFailed(true)}
      />
      {(image.illustration || image.kind === 'theme') && (
        <figcaption>
          {image.illustration ? '主题插画 · 非新闻现场' : '主题配图 · 非本次事件现场'}
        </figcaption>
      )}
    </figure>
  );
}
export function SocietyPicture({
  article,
  edition,
  unavailable,
  onUnavailable,
}: {
  article: Article;
  edition: Edition;
  unavailable: boolean;
  onUnavailable: () => void;
}) {
  const image = articleImage(article, edition);
  const showNews = image && !image.illustration && !unavailable;
  return (
    <figure className="society-illustration">
      <img
        src={showNews ? image.src : cityIllustration}
        alt={showNews ? image.caption : 'AI 主题插画：香港与内地城市天际线隔水相连，非新闻现场'}
        width={showNews ? image.width : 1448}
        height={showNews ? image.height : 1086}
        loading="lazy"
        referrerPolicy="no-referrer"
        onError={showNews ? onUnavailable : undefined}
      />
      {(!showNews || image.kind === 'theme') && (
        <figcaption>{!showNews ? '主题插画 · 非新闻现场' : '主题配图 · 非本次事件现场'}</figcaption>
      )}
    </figure>
  );
}
export function StoryBrief({
  article,
  edition,
  label,
  framed = false,
}: {
  article: Article;
  edition: Edition;
  label: string;
  framed?: boolean;
}) {
  const href = articleHref(editionBase(edition.date, edition.simulation), article.id);
  return (
    <article className={'story-brief' + (framed ? ' framed-story' : '')}>
      <div className="kicker">{label}</div>
      <h3>
        <a href={href} title={article.title}>
          {frontTitle(article.title)}
        </a>
      </h3>
      <p>{brief(article.summary, 120)}</p>
    </article>
  );
}
export function SourceList({ edition, ids }: { edition: Edition; ids?: string[] }) {
  const sources = ids
    ? edition.sources.filter((source) => ids.includes(source.id))
    : edition.sources;
  return (
    <section className="source-list">
      <h2>{ids ? '本文来源' : '全部采集来源'}</h2>
      <ol>
        {sources.map((source) => (
          <li key={source.id} value={edition.sources.indexOf(source) + 1}>
            <a href={source.url} target="_blank" rel="noopener noreferrer">
              {source.title}
            </a>
            {source.image?.page_url && (
              <small>
                图片出处：
                <a href={source.image.page_url} target="_blank" rel="noopener noreferrer">
                  {source.image.credit}
                </a>
              </small>
            )}
            <small>
              发布 {source.published_at.slice(0, 10)} · 采集 {source.captured_at.slice(0, 10)}{' '}
              {time(source.captured_at)}
            </small>
          </li>
        ))}
      </ol>
    </section>
  );
}
