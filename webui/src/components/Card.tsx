import type { HTMLAttributes, ReactNode } from 'react';

interface CardProps extends HTMLAttributes<HTMLDivElement> {
  /** 无内边距（用于列表类内容自己控制留白） */
  flush?: boolean;
  /** 紧凑内边距 */
  tight?: boolean;
  children: ReactNode;
}

export function Card({ flush, tight, children, className, ...rest }: CardProps) {
  const classes = ['card', flush ? 'card-flush' : '', tight ? 'card-tight' : '', className ?? '']
    .filter(Boolean)
    .join(' ');
  return (
    <div className={classes} {...rest}>
      {children}
    </div>
  );
}

interface CardHeadProps {
  title: ReactNode;
  hint?: ReactNode;
  extra?: ReactNode;
}

export function CardHead({ title, hint, extra }: CardHeadProps) {
  return (
    <div className="card-head">
      <div>
        <h2>{title}</h2>
        {hint ? <div className="card-hint">{hint}</div> : null}
      </div>
      {extra}
    </div>
  );
}

interface SectionTitleProps {
  title: ReactNode;
  sub?: ReactNode;
  extra?: ReactNode;
}

export function SectionTitle({ title, sub, extra }: SectionTitleProps) {
  return (
    <div className="section-title">
      <div>
        <h2>{title}</h2>
        {sub ? <div className="card-hint">{sub}</div> : null}
      </div>
      {extra}
    </div>
  );
}
