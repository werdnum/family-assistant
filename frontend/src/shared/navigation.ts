import {
  AlertTriangle,
  Calendar,
  Cog,
  FileText,
  FolderOpen,
  HelpCircle,
  History,
  Home,
  Info,
  MessageCircle,
  Mic,
  Search,
  Settings,
  Upload,
  Zap,
} from 'lucide-react';

export interface NavigationItem {
  type: 'section' | 'external' | 'link' | 'current';
  title: string;
  href?: string;
  to?: string;
  icon?: React.ComponentType<{ className?: string }>;
}

export const getNavigationItems = (currentPage?: string): NavigationItem[] => [
  { type: 'section', title: 'Main' },
  {
    type: currentPage === 'home' ? 'current' : 'link',
    to: '/',
    title: 'Home',
    icon: Home,
  },
  { type: 'section', title: 'Data' },
  { type: 'external', href: '/notes', title: 'Notes', icon: FileText },
  { type: 'link', to: '/context', title: 'Context', icon: FileText },
  { type: 'section', title: 'Documents' },
  { type: 'external', href: '/documents/', title: 'List', icon: FolderOpen },
  { type: 'external', href: '/documents/upload', title: 'Upload', icon: Upload },
  { type: 'external', href: '/vector-search', title: 'Search', icon: Search },
  { type: 'section', title: 'Communication' },
  {
    type: currentPage === 'chat' ? 'current' : 'link',
    to: '/chat',
    title: 'Chat',
    icon: MessageCircle,
  },
  {
    type: currentPage === 'voice' ? 'current' : 'link',
    to: '/voice',
    title: 'Voice',
    icon: Mic,
  },
  { type: 'external', href: '/history', title: 'History', icon: History },
  { type: 'section', title: 'Automation' },
  {
    type: currentPage === 'automations' ? 'current' : 'link',
    to: '/automations',
    title: 'Automations',
    icon: Zap,
  },
  { type: 'external', href: '/events', title: 'Events', icon: Calendar },
  { type: 'section', title: 'Internal' },
  {
    type: currentPage === 'tools' ? 'current' : 'link',
    to: '/tools',
    title: 'Tools',
    icon: Cog,
  },
  { type: 'external', href: '/tasks', title: 'Task Queue', icon: Settings },
  {
    type: currentPage === 'errors' ? 'current' : 'link',
    to: '/errors',
    title: 'Error Logs',
    icon: AlertTriangle,
  },
  { type: 'section', title: 'Help' },
  { type: 'external', href: '/docs/', title: 'Help', icon: HelpCircle },
  {
    type: currentPage === 'about' ? 'current' : 'link',
    to: '/about',
    title: 'About',
    icon: Info,
  },
];
