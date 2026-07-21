# 前端集成指南 - 用户与工作区系统

## 一、技术栈建议

### 推荐方案
- **框架**: React 18+ / Vue 3+ / Next.js 14+
- **状态管理**: Zustand / Pinia / Redux Toolkit
- **路由**: React Router v6 / Vue Router v4 / Next.js App Router
- **HTTP 客户端**: Axios / Fetch API
- **UI 组件库**: Ant Design / Material-UI / shadcn/ui

---

## 二、核心状态管理

### 2.1 认证状态 (Auth Store)

```typescript
// stores/authStore.ts
interface User {
  id: number;
  username: string;
  email: string;
  full_name: string | null;
  avatar_url: string | null;
}

interface AuthState {
  // 状态
  user: User | null;
  workspaceId: number | null;
  accessToken: string | null;
  refreshToken: string | null;
  isAuthenticated: boolean;
  
  // Actions
  setAuth: (data: {
    user: User;
    workspace_id: number;
    access_token: string;
    refresh_token: string;
  }) => void;
  clearAuth: () => void;
  switchWorkspace: (workspaceId: number) => Promise<void>;
}

// Zustand 示例
import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      user: null,
      workspaceId: null,
      accessToken: null,
      refreshToken: null,
      isAuthenticated: false,
      
      setAuth: (data) => {
        set({
          user: data.user,
          workspaceId: data.workspace_id,
          accessToken: data.access_token,
          refreshToken: data.refresh_token,
          isAuthenticated: true,
        });
      },
      
      clearAuth: () => {
        set({
          user: null,
          workspaceId: null,
          accessToken: null,
          refreshToken: null,
          isAuthenticated: false,
        });
      },
      
      switchWorkspace: async (workspaceId: number) => {
        const response = await api.post('/auth/switch-workspace', {
          workspace_id: workspaceId,
        });
        set({
          workspaceId: workspaceId,
          accessToken: response.data.access_token,
          refreshToken: response.data.refresh_token,
        });
      },
    }),
    {
      name: 'auth-storage', // LocalStorage key
      partialize: (state) => ({
        user: state.user,
        workspaceId: state.workspaceId,
        accessToken: state.accessToken,
        refreshToken: state.refreshToken,
        isAuthenticated: state.isAuthenticated,
      }),
    }
  )
);
```

### 2.2 工作区状态 (Workspace Store)

```typescript
// stores/workspaceStore.ts
interface Workspace {
  id: number;
  name: string;
  slug: string;
  display_name: string;
  workspace_type: 'personal' | 'team' | 'enterprise';
  logo_url: string | null;
}

interface WorkspaceState {
  workspaces: Workspace[];
  currentWorkspace: Workspace | null;
  
  fetchWorkspaces: () => Promise<void>;
  setCurrentWorkspace: (workspace: Workspace) => void;
}

export const useWorkspaceStore = create<WorkspaceState>((set) => ({
  workspaces: [],
  currentWorkspace: null,
  
  fetchWorkspaces: async () => {
    const response = await api.get('/workspaces');
    set({ workspaces: response.data.data });
  },
  
  setCurrentWorkspace: (workspace) => {
    set({ currentWorkspace: workspace });
  },
}));
```

---

## 三、API 客户端配置

### 3.1 Axios 拦截器

```typescript
// api/client.ts
import axios from 'axios';
import { useAuthStore } from '@/stores/authStore';

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
});

// 请求拦截器 - 自动添加 Token
api.interceptors.request.use(
  (config) => {
    const { accessToken } = useAuthStore.getState();
    if (accessToken) {
      config.headers.Authorization = `Bearer ${accessToken}`;
    }
    return config;
  },
  (error) => Promise.reject(error)
);

// 响应拦截器 - 处理 401 和 Token 刷新
api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const originalRequest = error.config;
    
    // Token 过期，尝试刷新
    if (error.response?.status === 401 && !originalRequest._retry) {
      originalRequest._retry = true;
      
      try {
        const { refreshToken } = useAuthStore.getState();
        const response = await axios.post('/auth/refresh', {
          refresh_token: refreshToken,
        });
        
        const { access_token, refresh_token } = response.data;
        useAuthStore.getState().setAuth({
          ...useAuthStore.getState(),
          access_token,
          refresh_token,
        });
        
        // 重试原请求
        originalRequest.headers.Authorization = `Bearer ${access_token}`;
        return api(originalRequest);
      } catch (refreshError) {
        // 刷新失败，跳转到登录页
        useAuthStore.getState().clearAuth();
        window.location.href = '/login';
        return Promise.reject(refreshError);
      }
    }
    
    return Promise.reject(error);
  }
);

export default api;
```

### 3.2 API 方法封装

```typescript
// api/auth.ts
import api from './client';

export const authApi = {
  // 注册
  register: async (data: {
    email: string;
    username: string;
    password: string;
    full_name?: string;
  }) => {
    const response = await api.post('/auth/register', data);
    return response.data;
  },
  
  // 登录
  login: async (data: { email: string; password: string }) => {
    const response = await api.post('/auth/login', data);
    return response.data;
  },
  
  // 刷新 Token
  refreshToken: async (refreshToken: string) => {
    const response = await api.post('/auth/refresh', {
      refresh_token: refreshToken,
    });
    return response.data;
  },
  
  // 切换工作区
  switchWorkspace: async (workspaceId: number) => {
    const response = await api.post('/auth/switch-workspace', {
      workspace_id: workspaceId,
    });
    return response.data;
  },
};

// api/workspaces.ts
export const workspaceApi = {
  // 获取工作区列表
  list: async () => {
    const response = await api.get('/workspaces');
    return response.data.data;
  },
  
  // 创建工作区
  create: async (data: {
    name: string;
    slug: string;
    display_name: string;
    workspace_type: string;
  }) => {
    const response = await api.post('/workspaces', data);
    return response.data.data;
  },
  
  // 邀请成员
  inviteMember: async (workspaceId: number, email: string) => {
    const response = await api.post(
      `/workspaces/${workspaceId}/members/invite`,
      { email }
    );
    return response.data.data;
  },
};

// api/sessions.ts
export const sessionApi = {
  // 发送消息
  sendMessage: async (data: {
    session_id: number | null;
    content: string;
  }) => {
    // 注意：这个接口返回 SSE 流
    const response = await fetch(`${API_BASE_URL}/sessions/messages`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${accessToken}`,
      },
      body: JSON.stringify(data),
    });
    return response.body; // ReadableStream
  },
  
  // 获取会话列表
  list: async () => {
    const response = await api.get('/sessions');
    return response.data.data;
  },
  
  // 获取消息历史
  getMessages: async (sessionId: number) => {
    const response = await api.get(`/sessions/${sessionId}/messages`);
    return response.data.data;
  },
};
```

---

## 四、路由与页面跳转

### 4.1 路由配置 (React Router 示例)

```typescript
// router/index.tsx
import { createBrowserRouter, Navigate } from 'react-router-dom';
import { useAuthStore } from '@/stores/authStore';

// 路由守卫
const ProtectedRoute = ({ children }: { children: React.ReactNode }) => {
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);
  
  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }
  
  return <>{children}</>;
};

const GuestRoute = ({ children }: { children: React.ReactNode }) => {
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);
  
  if (isAuthenticated) {
    return <Navigate to="/chat" replace />;
  }
  
  return <>{children}</>;
};

export const router = createBrowserRouter([
  // 公开路由
  {
    path: '/',
    element: <Navigate to="/login" replace />,
  },
  {
    path: '/login',
    element: (
      <GuestRoute>
        <LoginPage />
      </GuestRoute>
    ),
  },
  {
    path: '/register',
    element: (
      <GuestRoute>
        <RegisterPage />
      </GuestRoute>
    ),
  },
  
  // 需要认证的路由
  {
    path: '/chat',
    element: (
      <ProtectedRoute>
        <ChatLayout />
      </ProtectedRoute>
    ),
    children: [
      {
        index: true,
        element: <ChatPage />,
      },
      {
        path: ':sessionId',
        element: <ChatPage />,
      },
    ],
  },
  {
    path: '/workspaces',
    element: (
      <ProtectedRoute>
        <WorkspaceLayout />
      </ProtectedRoute>
    ),
    children: [
      {
        index: true,
        element: <WorkspaceListPage />,
      },
      {
        path: ':workspaceId',
        element: <WorkspaceDetailPage />,
      },
      {
        path: ':workspaceId/members',
        element: <WorkspaceMembersPage />,
      },
    ],
  },
  {
    path: '/profile',
    element: (
      <ProtectedRoute>
        <ProfilePage />
      </ProtectedRoute>
    ),
  },
]);
```

### 4.2 Next.js App Router 示例

```typescript
// app/layout.tsx
export default function RootLayout({ children }) {
  return (
    <html>
      <body>
        <AuthProvider>
          {children}
        </AuthProvider>
      </body>
    </html>
  );
}

// middleware.ts
import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

export function middleware(request: NextRequest) {
  const token = request.cookies.get('access_token');
  
  // 保护需要认证的路由
  if (request.nextUrl.pathname.startsWith('/chat')) {
    if (!token) {
      return NextResponse.redirect(new URL('/login', request.url));
    }
  }
  
  // 已登录用户重定向
  if (request.nextUrl.pathname === '/login' && token) {
    return NextResponse.redirect(new URL('/chat', request.url));
  }
  
  return NextResponse.next();
}

export const config = {
  matcher: ['/((?!api|_next/static|_next/image|favicon.ico).*)'],
};
```

---

## 五、页面流程与交互

### 5.1 注册流程

```typescript
// pages/RegisterPage.tsx
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuthStore } from '@/stores/authStore';
import { authApi } from '@/api/auth';

export default function RegisterPage() {
  const navigate = useNavigate();
  const setAuth = useAuthStore((state) => state.setAuth);
  const [loading, setLoading] = useState(false);
  
  const handleRegister = async (values: {
    email: string;
    username: string;
    password: string;
    full_name?: string;
  }) => {
    setLoading(true);
    try {
      const response = await authApi.register(values);
      
      // 保存认证信息
      setAuth({
        user: response.user,
        workspace_id: response.workspace_id,
        access_token: response.access_token,
        refresh_token: response.refresh_token,
      });
      
      // 跳转到聊天页面
      navigate('/chat', { replace: true });
      
      // 可选：显示欢迎提示
      showNotification({
        type: 'success',
        message: `欢迎，${response.user.username}！`,
        description: `已为您创建个人工作区`,
      });
    } catch (error) {
      showNotification({
        type: 'error',
        message: '注册失败',
        description: error.response?.data?.message || '请稍后重试',
      });
    } finally {
      setLoading(false);
    }
  };
  
  return (
    <div className="register-container">
      <Form onSubmit={handleRegister}>
        {/* 表单字段 */}
      </Form>
    </div>
  );
}
```

### 5.2 登录流程

```typescript
// pages/LoginPage.tsx
export default function LoginPage() {
  const navigate = useNavigate();
  const setAuth = useAuthStore((state) => state.setAuth);
  const [loading, setLoading] = useState(false);
  
  const handleLogin = async (values: {
    email: string;
    password: string;
  }) => {
    setLoading(true);
    try {
      const response = await authApi.login(values);
      
      // 保存认证信息
      setAuth({
        user: response.user,
        workspace_id: response.workspace_id,
        access_token: response.access_token,
        refresh_token: response.refresh_token,
      });
      
      // 跳转到聊天页面
      navigate('/chat', { replace: true });
    } catch (error) {
      showNotification({
        type: 'error',
        message: '登录失败',
        description: error.response?.data?.message || '邮箱或密码错误',
      });
    } finally {
      setLoading(false);
    }
  };
  
  return (
    <div className="login-container">
      <Form onSubmit={handleLogin}>
        {/* 表单字段 */}
      </Form>
    </div>
  );
}
```

### 5.3 工作区切换

```typescript
// components/WorkspaceSwitcher.tsx
export default function WorkspaceSwitcher() {
  const { workspaceId, switchWorkspace } = useAuthStore();
  const { workspaces, fetchWorkspaces } = useWorkspaceStore();
  const navigate = useNavigate();
  
  useEffect(() => {
    fetchWorkspaces();
  }, []);
  
  const handleSwitch = async (newWorkspaceId: number) => {
    try {
      await switchWorkspace(newWorkspaceId);
      
      // 刷新当前页面数据或跳转
      navigate('/chat', { replace: true });
      window.location.reload(); // 简单方案：重新加载
      
      showNotification({
        type: 'success',
        message: '工作区已切换',
      });
    } catch (error) {
      showNotification({
        type: 'error',
        message: '切换失败',
      });
    }
  };
  
  return (
    <Dropdown>
      <Dropdown.Trigger>
        <Button>
          {workspaces.find(w => w.id === workspaceId)?.display_name}
        </Button>
      </Dropdown.Trigger>
      <Dropdown.Menu>
        {workspaces.map((workspace) => (
          <Dropdown.Item
            key={workspace.id}
            onClick={() => handleSwitch(workspace.id)}
            active={workspace.id === workspaceId}
          >
            {workspace.display_name}
            {workspace.workspace_type === 'personal' && ' (个人)'}
          </Dropdown.Item>
        ))}
      </Dropdown.Menu>
    </Dropdown>
  );
}
```

### 5.4 聊天页面

```typescript
// pages/ChatPage.tsx
export default function ChatPage() {
  const { workspaceId } = useAuthStore();
  const [sessions, setSessions] = useState([]);
  const [currentSessionId, setCurrentSessionId] = useState<number | null>(null);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  
  // 加载会话列表（自动过滤当前工作区）
  useEffect(() => {
    loadSessions();
  }, [workspaceId]);
  
  const loadSessions = async () => {
    const data = await sessionApi.list();
    setSessions(data);
  };
  
  // 加载消息历史
  useEffect(() => {
    if (currentSessionId) {
      loadMessages(currentSessionId);
    }
  }, [currentSessionId]);
  
  const loadMessages = async (sessionId: number) => {
    const data = await sessionApi.getMessages(sessionId);
    setMessages(data);
  };
  
  // 发送消息
  const handleSend = async () => {
    const response = await fetch(`${API_BASE_URL}/sessions/messages`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${accessToken}`,
      },
      body: JSON.stringify({
        session_id: currentSessionId,
        content: input,
      }),
    });
    
    const reader = response.body?.getReader();
    const decoder = new TextDecoder();
    
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      
      const chunk = decoder.decode(value);
      const lines = chunk.split('\n\n');
      
      for (const line of lines) {
        if (line.startsWith('data: ')) {
          const data = JSON.parse(line.slice(6));
          
          // 处理不同事件类型
          if (data.type === 'session_created') {
            setCurrentSessionId(data.session_id);
          } else if (data.type === 'text_delta') {
            // 更新消息显示
          }
        }
      }
    }
  };
  
  return (
    <div className="chat-container">
      <Sidebar sessions={sessions} onSelect={setCurrentSessionId} />
      <ChatArea messages={messages} onSend={handleSend} />
    </div>
  );
}
```

---

## 六、关键交互时序

### 6.1 首次注册流程

```
用户填写注册表单
  ↓
提交 POST /auth/register
  ↓
后端返回 { user, workspace_id, access_token, refresh_token }
  ↓
前端保存到 AuthStore (LocalStorage 持久化)
  ↓
自动跳转到 /chat
  ↓
显示欢迎提示 + 新手引导(可选)
```

### 6.2 登录后进入系统

```
用户填写登录表单
  ↓
提交 POST /auth/login
  ↓
后端返回 { user, workspace_id, access_token, refresh_token }
  ↓
前端保存到 AuthStore
  ↓
跳转到 /chat
  ↓
加载会话列表 GET /sessions (自动带 workspace_id)
  ↓
显示当前工作区的会话
```

### 6.3 切换工作区流程

```
用户点击工作区切换器
  ↓
显示工作区列表 (从 WorkspaceStore)
  ↓
用户选择新工作区
  ↓
提交 POST /auth/switch-workspace { workspace_id }
  ↓
后端返回新的 { access_token, refresh_token }
  ↓
前端更新 AuthStore
  ↓
刷新当前页面数据 (或重新加载)
  ↓
显示新工作区的会话列表
```

### 6.4 创建会话流程

```
用户在聊天输入框输入消息
  ↓
提交 POST /sessions/messages { session_id: null, content: "..." }
  ↓
后端:
  - 从 token 提取 user_id 和 workspace_id
  - 创建新 session 记录
  - 返回 SSE 流
  ↓
前端:
  - 接收 session_created 事件 → 更新 currentSessionId
  - 接收 text_delta 事件 → 逐字显示响应
  - 接收 done 事件 → 完成
  ↓
会话列表自动刷新，显示新会话
```

---

## 七、UI/UX 建议

### 7.1 布局结构

```
┌─────────────────────────────────────────────────┐
│ Header                                          │
│  [Logo] [工作区切换器 ▼] ... [用户菜单 ▼]        │
├──────────┬──────────────────────────────────────┤
│          │                                      │
│ Sidebar  │  Main Content Area                   │
│          │                                      │
│ 会话列表  │  聊天区域 / 工作区设置 / 其他页面       │
│          │                                      │
│ [新会话]  │                                      │
│          │                                      │
└──────────┴──────────────────────────────────────┘
```

### 7.2 工作区切换器设计

```typescript
// 位置：顶部导航栏
<WorkspaceSwitcher>
  <Trigger>
    <Avatar src={currentWorkspace.logo_url} />
    <span>{currentWorkspace.display_name}</span>
    <ChevronDown />
  </Trigger>
  
  <Menu>
    <Section title="个人工作区">
      {personalWorkspaces.map(ws => (
        <Item key={ws.id} onClick={() => switchTo(ws.id)}>
          {ws.display_name}
        </Item>
      ))}
    </Section>
    
    <Section title="团队工作区">
      {teamWorkspaces.map(ws => (
        <Item key={ws.id} onClick={() => switchTo(ws.id)}>
          {ws.display_name}
        </Item>
      ))}
    </Section>
    
    <Divider />
    
    <Item onClick={() => navigate('/workspaces/create')}>
      <Plus /> 创建新工作区
    </Item>
  </Menu>
</WorkspaceSwitcher>
```

### 7.3 会话列表设计

```typescript
// 按工作区自动过滤
<SessionList>
  <Header>
    <h2>会话列表</h2>
    <Button onClick={createNew}>新会话</Button>
  </Header>
  
  <List>
    {sessions.map(session => (
      <SessionItem
        key={session.id}
        active={session.id === currentSessionId}
        onClick={() => setCurrentSession(session.id)}
      >
        <Title>{session.title}</Title>
        <Meta>
          {session.workspace_id === currentWorkspace.id && (
            <Badge>当前工作区</Badge>
          )}
          <Time>{formatTime(session.last_active_at)}</Time>
        </Meta>
      </SessionItem>
    ))}
  </List>
</SessionList>
```

---

## 八、注意事项

### 8.1 Token 过期处理

- 使用拦截器自动刷新 Token
- 刷新失败后跳转到登录页
- 保存原始请求，刷新后重试

### 8.2 工作区切换

- 切换后需要刷新所有依赖工作区的数据
- 建议简单方案：`window.location.reload()`
- 高级方案：使用全局事件总线，各组件监听并重新加载

### 8.3 SSE 流处理

- 使用 `fetch` API 而非 Axios（更好的流支持）
- 逐行解析 `data: ` 前缀的 JSON
- 处理连接中断和重连

### 8.4 状态持久化

- 使用 `zustand/persist` 或 `localStorage`
- 敏感信息考虑加密存储
- Token 存储在 `httpOnly` Cookie（需要后端配合）

### 8.5 错误处理

- 统一错误提示组件
- 网络错误重试机制
- 友好的错误信息展示

---

## 九、完整示例：React + Zustand + React Router

```bash
# 项目结构
src/
├── api/
│   ├── client.ts          # Axios 配置
│   ├── auth.ts            # 认证 API
│   ├── workspaces.ts      # 工作区 API
│   └── sessions.ts        # 会话 API
├── stores/
│   ├── authStore.ts       # 认证状态
│   └── workspaceStore.ts  # 工作区状态
├── pages/
│   ├── LoginPage.tsx
│   ├── RegisterPage.tsx
│   ├── ChatPage.tsx
│   └── WorkspacePage.tsx
├── components/
│   ├── WorkspaceSwitcher.tsx
│   ├── SessionList.tsx
│   └── ChatArea.tsx
├── router/
│   └── index.tsx          # 路由配置
└── App.tsx
```

---

## 十、总结

前端需要实现的核心功能：

✅ **认证状态管理** - 保存 user、workspace_id、tokens  
✅ **Token 自动注入** - Axios 拦截器  
✅ **Token 自动刷新** - 401 响应拦截  
✅ **路由守卫** - 未登录跳转到 /login  
✅ **工作区切换** - 切换后刷新数据  
✅ **会话列表** - 自动过滤当前工作区  
✅ **SSE 流式响应** - 实时显示 Agent 输出  

所有数据隔离和权限验证由后端处理，前端只需要：
1. 保存 Token 并自动注入请求头
2. 根据响应的 `workspace_id` 更新 UI
3. 提供工作区切换入口
4. 刷新页面数据以反映新的工作区上下文
