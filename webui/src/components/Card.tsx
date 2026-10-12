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
