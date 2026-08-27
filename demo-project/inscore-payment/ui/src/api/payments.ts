import axios from 'axios'

const BASE = import.meta.env.VITE_API_URL ?? 'https://api.inscore.aig.com'

export interface PaymentRequest {
  policy_id: string
  amount: string
  method: 'credit_card' | 'ach' | 'wire' | 'check'
  currency?: string
  description?: string
}

export interface PaymentResponse {
  payment_id: string
  policy_id: string
  amount: string
  status: 'pending' | 'processing' | 'completed' | 'failed' | 'refunded' | 'cancelled'
  method: string
  created_at: string
  processed_at?: string
  transaction_ref?: string
}

export interface PaymentSummary {
  policy_id: string
  total_paid: string
  total_pending: string
  payment_count: number
  last_payment_date?: string
  next_due_date?: string
}

const client = axios.create({ baseURL: BASE })

export const paymentsApi = {
  create: (req: PaymentRequest): Promise<PaymentResponse> =>
    client.post('/payments', req).then(r => r.data),

  getById: (id: string): Promise<PaymentResponse> =>
    client.get(`/payments/${id}`).then(r => r.data),

  getByPolicy: (policyId: string): Promise<PaymentResponse[]> =>
    client.get(`/payments/policy/${policyId}`).then(r => r.data),

  getSummary: (policyId: string): Promise<PaymentSummary> =>
    client.get(`/payments/policy/${policyId}/summary`).then(r => r.data),

  refund: (paymentId: string, reason: string, amount?: string): Promise<PaymentResponse> =>
    client.post(`/payments/${paymentId}/refund`, { payment_id: paymentId, reason, amount }).then(r => r.data),

  cancel: (paymentId: string): Promise<void> =>
    client.post(`/payments/${paymentId}/cancel`).then(r => r.data),
}
