import type { Edition } from '../types';
import { useState } from 'react';
import { articleHref, brief, editionBase } from '../routes';
import { headlineArticle, societyArticles } from '../media';
import { Header, Footer } from '../components/PageLayout';
import {
  EditionNote,
  Photo,
  SocietyPicture,
  StoryBrief,
  SectionTitle,
} from '../components/EditionContent';
import { frontTitle } from '../utils/headline';

export function FrontPage({ edition: e }: { edition: Edition }) {
  const [unavailableImages, setUnavailableImages] = useState<string[]>([]);
  const base = editionBase(e.date, e.simulation),
    lead = headlineArticle(e),
    secondary = e.articles.ai.filter((article) => article.id !== lead.id).slice(0, 2);
  const isAiLead = e.articles.ai.includes(lead);
  const society = societyArticles(e, unavailableImages);
  const cityLead = society[0];
  return (
    <>
      <EditionNote edition={e} />
      <Header edition={e} />
      <main className="front-page">
        <section className="front-lead" id="ai" aria-label="今日头条">
          <article className="lead-story">
            <div className="kicker">
              <span className="rubric">今日头条</span>{' '}
              {isAiLead ? 'AI & TECHNOLOGY' : 'SOCIETY & PUBLIC AFFAIRS'}
            </div>
            <a
              className="lead-image-link"
              href={articleHref(base, lead.id)}
              aria-label={'阅读：' + lead.title}
            >
              <Photo article={lead} edition={e} large />
            </a>
            <h1>
              <a href={articleHref(base, lead.id)} title={lead.title}>
                {frontTitle(lead.title)}
              </a>
            </h1>
            <p className="lead-deck">{brief(lead.summary, 180)}</p>
          </article>
          <aside className="lead-sidebar">
            <div className="column-label">
              科技短讯 <span>AI BRIEFING</span>
            </div>
            {secondary.map((article, index) => (
              <StoryBrief
                key={article.id}
                article={article}
                edition={e}
                label={'AI 观察 / ' + String(index + 1).padStart(2, '0')}
              />
            ))}
          </aside>
        </section>
        <section id="society" className="society-section">
          <SectionTitle title="内地与香港" english="SOCIETY & PUBLIC AFFAIRS" />
          <div className="society-layout">
            <div className="society-feature">
              <StoryBrief article={cityLead} edition={e} label="社会观察 / 01" />
              <SocietyPicture
                key={cityLead.id}
                article={cityLead}
                edition={e}
                unavailable={unavailableImages.includes(cityLead.id)}
                onUnavailable={() => setUnavailableImages((ids) => [...ids, cityLead.id])}
              />
            </div>
            <div className="brief-grid">
              {society
                .filter((article) => article.id !== cityLead.id)
                .map((article, index) => (
                  <StoryBrief
                    key={article.id}
                    article={article}
                    edition={e}
                    label={'社会观察 / 0' + (index + 2)}
                    framed={index === 0 || index === 3}
                  />
                ))}
            </div>
          </div>
        </section>
        <section id="markets" className="market-digest">
          <div className="market-heading">
            <SectionTitle title="两地市场" english="MARKETS" />
            <div className="market-summary-date">
              <time dateTime={e.date}>{e.date}</time>
              <small>收盘行情</small>
            </div>
          </div>
          {(['a', 'hk'] as const).map((key) => {
            const market = e.markets[key];
            const index = market.indices[0];
            return (
              <a href={base + '/markets#' + key} className="market-snapshot" key={key}>
                <span>{key === 'a' ? 'A股' : '港股'}</span>
                <span className="market-summary-index">
                  {index ? (
                    <>
                      {index.name}
                      <b>{index.close.toFixed(2)}</b>
                    </>
                  ) : (
                    '暂无已核实行情'
                  )}
                </span>
                <strong
                  className={
                    index && market.status === 'open'
                      ? index.change_pct > 0
                        ? 'up'
                        : index.change_pct < 0
                          ? 'down'
                          : ''
                      : ''
                  }
                >
                  {market.status === 'unavailable'
                    ? '数据暂缺'
                    : market.status === 'closed'
                      ? '休市'
                      : market.indices[0]
                        ? market.indices[0].change_pct.toFixed(2) + '%'
                        : '收盘观察'}
                </strong>
              </a>
            );
          })}
        </section>
      </main>
      <Footer edition={e} />
    </>
  );
}
