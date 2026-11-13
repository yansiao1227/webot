import type { Market, Sector } from '../types';
const plus = (n: number) => (n > 0 ? '+' : '') + n.toFixed(2) + '%';
const direction = (n: number) => (n > 0 ? 'up' : n < 0 ? 'down' : 'neutral');
const time = (iso: string) =>
  new Date(iso).toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    timeZone: 'Asia/Shanghai',
  });
function Ranking({ title, list, kind }: { title: string; list: Sector[]; kind: 'up' | 'down' }) {
  return (
    <div className="ranking">
      <h4>
        <span className={'dot ' + kind}></span>
        {title}
      </h4>
      {list.length ? (
        <ol>
          {list.map((s, i) => (
            <li key={s.code}>
              <span className="rank-no">{String(i + 1).padStart(2, '0')}</span>
              <span>{s.name}</span>
              <b className={kind}>{plus(s.change_pct)}</b>
            </li>
          ))}
        </ol>
      ) : (
        <p className="muted">无符合条件的行业数据</p>
      )}
    </div>
  );
}
export function MarketBlock({ market, title }: { market: Market; title: string }) {
  const closed = market.status === 'closed';
  return (
    <section className="market-block">
      <div className="market-title">
        <h3>{title}</h3>
        <span className="market-state">
          {closed ? '休市' : market.status === 'unavailable' ? '数据暂缺' : '收盘观察'}
        </span>
      </div>
      <p className="market-caption">
        {market.trade_date ? '交易日 ' + market.trade_date : '未取得可核实交易日'}
        {market.as_of ? ' · 截至 ' + time(market.as_of) : ''}
      </p>
      {market.indices.length > 0 && (
        <table className="index-table">
          <thead>
            <tr>
              <th>主要指数</th>
              <th>收盘点位</th>
              <th>涨跌幅</th>
            </tr>
          </thead>
          <tbody>
            {market.indices.map((q) => (
              <tr key={q.code}>
                <th>{q.name}</th>
                <td>
                  {q.close.toLocaleString('en-US', {
                    minimumFractionDigits: 2,
                    maximumFractionDigits: 2,
                  })}
                </td>
                <td className={direction(q.change_pct)}>{plus(q.change_pct)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {!closed && market.status !== 'unavailable' && (
        <div className="rankings">
          <Ranking title="领涨行业" list={market.gainers} kind="up" />
          <Ranking title="领跌行业" list={market.losers} kind="down" />
        </div>
      )}
      {closed && <p className="market-note">本市场休市，展示最近交易日数据；不生成当日行业榜。</p>}
      {market.missing.length > 0 && (
        <section className="market-limitations">
          <h4>数据说明</h4>
          <ol className="missing">
            {market.missing.map((m) => (
              <li key={m}>{m}</li>
            ))}
          </ol>
        </section>
      )}
      <p className="market-source">
        {market.classification || '行业口径未取得'}
        <br />
        {market.source_url ? (
          <a href={market.source_url} target="_blank" rel="noopener noreferrer">
            来源：{market.source}
          </a>
        ) : (
          market.source || '来源暂缺'
        )}
      </p>
    </section>
  );
}
