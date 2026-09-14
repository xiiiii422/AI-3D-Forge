import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { IconLoader } from './Icons';

export type ButtonVariant = 'default' | 'primary' | 'brand' | 'ghost' | 'danger';
export type ButtonSize = 'sm' | 'md' | 'lg';

interface BaseProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'className'> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** 运行中：禁用并显示持续旋转的图标 */
  loading?: boolean;
  /** 撑满容器宽度 */
  block?: boolean;
  icon?: ReactNode;
  iconRight?: ReactNode;
  children?: ReactNode;
  className?: string;
  /**
   * 纯图标按钮（无可见文字）。
   * 注意：iconOnly 时必须传 aria-label，组件会在开发模式下断言，
   * 这样既保证无障碍硬要求，又不必用联合类型把调用点写得很啰嗦。
   */
  iconOnly?: boolean;
}

export type ButtonProps = BaseProps;
const VARIANT_CLASS: Record<ButtonVariant, string> = {
  default: '',
  primary: 'btn-primary',
  brand: 'btn-brand',
  ghost: 'btn-ghost',
  danger: 'btn-danger',
};

const SIZE_CLASS: Record<ButtonSize, string> = {
  sm: 'btn-sm',
  md: '',
  lg: 'btn-lg',
};

export function Button(props: ButtonProps) {
  const {
    variant = 'default',
    size = 'md',
    loading = false,
    block = false,
    icon,
    iconRight,
    children,
    className,
    iconOnly,
    disabled,
    type = 'button',
    ...rest
  } = props;

  const classes = [
    'btn',
    VARIANT_CLASS[variant],
    SIZE_CLASS[size],
    block ? 'btn-block' : '',
    iconOnly ? 'btn-icon' : '',
    className ?? '',
  ]
    .filter(Boolean)
    .join(' ');

  if (import.meta.env.DEV && iconOnly && !rest['aria-label']) {
    // 无障碍硬要求：图标按钮必须有可读名称
    console.warn('[Button] iconOnly 按钮缺少 aria-label');
  }

  return (
    <button
      type={type}
      className={classes}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading ? <IconLoader className="spin" /> : icon}
      {!iconOnly && children}
      {!iconOnly && !loading && iconRight}
    </button>
  );
}
