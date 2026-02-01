import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  MessageSquare,
  Mic,
  StickyNote,
  FileText,
  Zap,
  History,
  Send,
  ArrowRight,
} from 'lucide-react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';

const LandingPage: React.FC = () => {
  const [prompt, setPrompt] = useState('');
  const navigate = useNavigate();

  const handleSearch = (e?: React.FormEvent) => {
    e?.preventDefault();
    if (prompt.trim()) {
      navigate(`/chat?q=${encodeURIComponent(prompt.trim())}`);
    } else {
      navigate('/chat');
    }
  };

  const menuItems = [
    {
      title: 'Chat',
      description: 'Start a new conversation with the AI assistant.',
      icon: <MessageSquare className="w-6 h-6 text-blue-500" />,
      link: '/chat',
      color: 'bg-blue-50 dark:bg-blue-900/20',
    },
    {
      title: 'Voice Mode',
      description: 'Talk to the assistant using natural speech.',
      icon: <Mic className="w-6 h-6 text-purple-500" />,
      link: '/voice',
      color: 'bg-purple-50 dark:bg-purple-900/20',
    },
    {
      title: 'Notes',
      description: 'Manage your personal notes and snippets.',
      icon: <StickyNote className="w-6 h-6 text-yellow-500" />,
      link: '/notes',
      color: 'bg-yellow-50 dark:bg-yellow-900/20',
    },
    {
      title: 'Documents',
      description: 'Upload and search through your documents.',
      icon: <FileText className="w-6 h-6 text-green-500" />,
      link: '/documents',
      color: 'bg-green-50 dark:bg-green-900/20',
    },
    {
      title: 'Automations',
      description: 'Configure and run automated workflows.',
      icon: <Zap className="w-6 h-6 text-orange-500" />,
      link: '/automations',
      color: 'bg-orange-50 dark:bg-orange-900/20',
    },
    {
      title: 'History',
      description: 'Review your past conversations and activity.',
      icon: <History className="w-6 h-6 text-gray-500" />,
      link: '/chat', // History is often inside chat
      color: 'bg-gray-50 dark:bg-gray-900/20',
    },
  ];

  const suggestions = [
    "What's on my schedule today?",
    'Summarize my recent notes',
    'Help me write an email to my boss',
    "Find documents related to 'taxes'",
  ];

  return (
    <div
      className="container mx-auto px-4 py-8 max-w-6xl min-h-[calc(100vh-4rem)] flex flex-col justify-center"
      data-app-ready="true"
    >
      <div className="text-center mb-12">
        <h1 className="text-4xl md:text-5xl font-extrabold tracking-tight mb-4 bg-gradient-to-r from-primary to-primary/60 bg-clip-text text-transparent">
          Family Assistant
        </h1>
        <p className="text-xl text-muted-foreground max-w-2xl mx-auto">
          Your personal companion for managing daily life, notes, and documents.
        </p>
      </div>

      <div className="max-w-3xl mx-auto w-full mb-16">
        <form onSubmit={handleSearch} className="relative group">
          <Input
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="How can I help you today?"
            className="h-14 pl-6 pr-16 text-lg rounded-2xl shadow-xl border-primary/20 focus:border-primary transition-all"
          />
          <Button
            type="submit"
            size="icon"
            className="absolute right-2 top-2 h-10 w-10 rounded-xl transition-transform group-focus-within:scale-105"
          >
            <Send className="w-5 h-5" />
          </Button>
        </form>

        <div className="mt-4 flex flex-wrap justify-center gap-2">
          {suggestions.map((s, i) => (
            <button
              key={i}
              onClick={() => {
                setPrompt(s);
                navigate(`/chat?q=${encodeURIComponent(s)}`);
              }}
              className="text-xs bg-secondary hover:bg-secondary/80 text-secondary-foreground px-3 py-1.5 rounded-full transition-colors flex items-center gap-1"
            >
              {s} <ArrowRight className="w-3 h-3" />
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        {menuItems.map((item, index) => (
          <Card
            key={index}
            className="group hover:shadow-lg transition-all cursor-pointer border-none shadow-md overflow-hidden"
            onClick={() => navigate(item.link)}
          >
            <CardHeader className="pb-2">
              <div
                className={`w-12 h-12 rounded-2xl ${item.color} flex items-center justify-center mb-2 group-hover:scale-110 transition-transform`}
              >
                {item.icon}
              </div>
              <CardTitle className="text-xl">{item.title}</CardTitle>
              <CardDescription className="text-sm leading-relaxed">
                {item.description}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div className="flex items-center text-primary text-sm font-medium opacity-0 group-hover:opacity-100 transition-opacity">
                Get started <ArrowRight className="w-4 h-4 ml-1" />
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
};

export default LandingPage;
