import { useState } from 'react'
import { paymentsApi, type PaymentRequest } from '../api/payments'

type Method = 'credit_card' | 'ach' | 'wire' | 'check'

interface PaymentFormProps {
  policyId: string
  onSuccess: (paymentId: string) => void
  onError: (message: string) => void
}

export function PaymentForm({ policyId, onSuccess, onError }: PaymentFormProps) {
  const [amount, setAmount] = useState('')
  const [method, setMethod] = useState<Method>('credit_card')
  const [loading, setLoading] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    const parsed = parseFloat(amount)
    if (isNaN(parsed) || parsed <= 0) {
      onError('Please enter a valid amount')
      return
    }
    setLoading(true)
    try {
      const req: PaymentRequest = {
        policy_id: policyId,
        amount: parsed.toFixed(2),
        method,
      }
      const result = await paymentsApi.create(req)
      onSuccess(result.payment_id)
    } catch (err: any) {
      onError(err.response?.data?.detail ?? 'Payment failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="payment-form">
      <h2>Make a Payment</h2>
      <p className="policy-id">Policy: {policyId}</p>

      <div className="form-group">
        <label htmlFor="amount">Payment Amount (USD)</label>
        <input
          id="amount"
          type="number"
          min="0.01"
          max="1000000"
          step="0.01"
          placeholder="0.00"
          value={amount}
          onChange={e => setAmount(e.target.value)}
          required
        />
      </div>

      <div className="form-group">
        <label htmlFor="method">Payment Method</label>
        <select
          id="method"
          value={method}
          onChange={e => setMethod(e.target.value as Method)}
        >
          <option value="credit_card">Credit Card</option>
          <option value="ach">ACH Bank Transfer</option>
          <option value="wire">Wire Transfer</option>
          <option value="check">Check</option>
        </select>
      </div>

      <button type="submit" disabled={loading} className="btn-primary">
        {loading ? 'Processing...' : `Pay $${amount || '0.00'}`}
      </button>
    </form>
  )
}

export default PaymentForm
