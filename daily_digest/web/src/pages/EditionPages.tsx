import type { Edition } from '../types';
import { articleHref, editionBase } from '../routes';
import { time } from '../utils/format';
import { MarketBlock } from '../components/MarketBlock';
import { Photo, SourceList } from '../components/EditionContent';
import { DetailFrame } from '../components/PageLayout';
import { Empty } from './Empty';

export function ArticlePage({ edition, id }: { edition: Edition; id: string }) {
  const category = edition.articles.ai.some((article) => article.id === id) ? 'ai' : 'society';
  const article = edition.articles[category].find((article) => article.id === id);
  if (!article) return <Empty missing />;
  const label = category === 'ai' ? 'AI 观察' : '内地与香港';
  const others = edition.articles[category].filter((item) => item.id !== id).slice(0, 2);
  return (
    <DetailFrame edition={edition} label={label}>
      <article className="full-article">
        <div className="kicker">{label} · 新闻与观察</div>
        <h1>{article.title}</h1>
        <div className="article-meta">内容截止 {time(edition.cutoff_at)}（北京）</div>
        <Photo key={article.id} article={article} edition={edition} large fallback={false} />
        {[
          ['背景', article.background],
          ['影响与观察', article.impact],
          ['仍待核实', article.uncertainty],
        ]
          .filter(([, text]) => text.trim())
          .map(([title, text]) => (
            <section className="prose-section" key={title}>
              <h2>{title}</h2>
              {text
                .split(/\n+/)
                .filter(Boolean)
                .map((paragraph, index) => (
                  <p key={index}>{paragraph}</p>
                ))}
            </section>
          ))}
        <SourceList edition={edition} ids={article.source_ids} />
      </article>
      {others.length > 0 && (
        <section className="related-stories">
          <h2>同版继续读</h2>
          {others.map((item) => (
            <a
              key={item.id}
              href={articleHref(editionBase(edition.date, edition.simulation), item.id)}
            >
              {item.title}
            </a>
          ))}
        </section>
      )}
    </DetailFrame>
  );
}
export function AnalysisPage({ edition, id }: { edition: Edition; id: string }) {
  const item = /^\d+$/.test(id) ? edition.analysis[Number(id)] : null;
  if (!item) return <Empty missing />;
  return (
    <DetailFrame edition={edition} label="观察手记">
      <article className="full-article">
        <div className="kicker">观察手记 / 分析与事实分开呈现</div>
        <h1>{item.title}</h1>
        <div className="article-meta">{edition.date} · 基于已采集公开资料</div>
        <div className="prose-section">
          {item.text
            .split(/\n+/)
            .filter(Boolean)
            .map((text, index) => (
              <p key={index}>{text}</p>
            ))}
        </div>
        <SourceList edition={edition} ids={item.source_ids} />
      </article>
    </DetailFrame>
  );
}
export function MarketsPage({ edition }: { edition: Edition }) {
  return (
    <DetailFrame edition={edition} label="两地市场">
      <h1 className="page-title">两地收盘观察</h1>
      <p className="page-deck">
        按市场分别核验交易日期与行业口径。盘中数据不当作收盘，缺项如实列明。
      </p>
      <div className="market-grid">
        <div id="a">
          <MarketBlock market={edition.markets.a} title="A股" />
        </div>
        <div id="hk">
          <MarketBlock market={edition.markets.hk} title="港股" />
        </div>
      </div>
      <p className="quiet-note">两地行业分类口径不同，分别排序；红色表示上涨，绿色表示下跌。</p>
    </DetailFrame>
  );
}
export function NotesPage({ edition }: { edition: Edition }) {
  return (
    <DetailFrame edition={edition} label="阅读说明">
      <h1 className="page-title">本期编辑说明与来源</h1>
      <p className="page-deck">{edition.intro}</p>
      {edition.warnings.length > 0 && (
        <section className="reading-notes">
          <h2>采集与核实的限制</h2>
          <ul>
            {edition.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </section>
      )}
      <p className="quiet-note">
        配图优先选取对应新闻的公开来源图片；自制配图在替代文本中标记为主题插画，不代表现场记录。来源可访问不等于已获得公开转载授权，当前仅用于本地预览核对。
      </p>
      <SourceList edition={edition} />
    </DetailFrame>
  );
}
