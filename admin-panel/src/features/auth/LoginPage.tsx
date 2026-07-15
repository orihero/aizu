import { Link } from 'react-router-dom';
import { useAuth } from '@/shared/hooks/useAuth';
import { AuthForm } from './AuthForm';

export function LoginPage() {
  const { login } = useAuth();
  return (
    <AuthForm
      mode="login"
      heading="Welcome back"
      subheading="Sign in to your AIZU workspace."
      submitLabel="Log in"
      onSubmit={(values) => login({ email: values.email, password: values.password })}
      footer={
        <>
          New to AIZU?{' '}
          <Link to="/signup" className="font-semibold text-brand hover:underline">
            Sign up
          </Link>
        </>
      }
    />
  );
}
